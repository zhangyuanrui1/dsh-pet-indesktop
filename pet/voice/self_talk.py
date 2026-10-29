# -*- coding: utf-8 -*-
"""点击台词 / 绑定台词的本地语音预缓存（后台补齐）。

**为什么要预缓存**：本机合成一句要几秒（自建 CosyVoice 更慢，20 秒上下，还要 ASR
回读校验、不满意就重抽），点击那一刻现合成必然卡顿。所以把点击会说出口的句子提前
落成音频文件，运行时直接播文件（见 ``AppShell.speak_self_talk``）：零延迟、断网可用、
不占常驻内存。

**用谁合成**：与「设置 → 语音报时 → 立即试听」同一条链路——``pet/tts`` 注册表里
**当前配置的后端与音色**（``voice_chime.synth_attempts``）。于是"试听有声音"就等价于
"台词语音能生成"。这里曾经自建了一条直连本机 CosyVoice（127.0.0.1:9880）的路，与注册表
完全脱钩：普通用户既没装那个服务、也得不到任何提示，表现就是"开关开了、没错误、一个
文件都没有"（issue #305）。自建服务只剩一个可选覆盖：``DSH_PET_TTS_URL`` 指向它时先走，
它不在线就**落回注册表**，不再静默收工。

**与外部的契约**（外部脚本按它产出，测试已钉死）：

    <配置目录>/self_talk_voice/<md5(文本 utf-8)[:16]>.<扩展名>       ← 经典文本缓存
    <配置目录>/self_talk_voice/<cache_key(文本, flavor)>.<扩展名>    ← 注册表产物

两种来源的身份是**分开**的，查找顺序「当前配置的产物 → 经典文本缓存」：

- 注册表生成的音频按 ``text + provider.flavor(实际合成输入的 values)`` 命名（与
  ``voice_chime`` 的报时缓存同一个 ``cache_key``）：换音色 / 语速 / 后端就换键，
  旧句子不会被当成"已缓存"，点击也不会命中另一把嗓子；
- 外部产出脚本（``F:\\dsh\\tts\\build_self_talk_voice.py``）与自建服务覆盖模式没有
  flavor，仍按纯文本命中（``cache_path`` 的老算法），**不能**因升级作废；但它也
  不能凌驾于当前配置生成的产物之上（否则换了音色仍旧播老声音）。

扩展名由产出它的后端决定（edge/doubao → mp3，sapi/mimo 与外部脚本 → wav）；
经典缓存的查找按 ``<md5>.*`` 枚举，不认死扩展名，外部脚本那份 wav 照样命中。


**名单**：每只宠**自己 slot 档案**里的自言自语（``self_talk_texts`` 是每宠键）＋该宠
各角色**点击动画绑定**台词（``character_profiles.<角色>.click_talk_bindings.<动画id>``），
跨宠按文本去重。读错配置层的后果是 slot≠0 的宠在**别人的名单**上生成、点击时命中不到
→ 回退在线合成，音色变成另一个人。

**开关**：``self_talk_voice_precache_enabled`` 默认关闭。开一次要在本机写几十个音频文件，
只有明确开启的用户才付这笔合成开销与磁盘占用。

**落盘是原子的**：先写同目录临时文件、再用 ``os.replace`` 换名，最终路径上只可能完整
文件或什么都不出现；查找侧还会核对 wav 头部声明的长度，半截产物既不上账也不被当成命中。

**导入是惰性的**：合成链（``pet.tts`` / ``voice_chime`` / ``config``）一律在函数里 import
——``import pet.tts`` 实测 0.35s，而本模块随 ``pet.app`` 在启动路径上被 import；预缓存
跑在后台线程，这笔成本放到那时付（与 ``voice_chime_service`` 的惰性口径一致）。
``cached_audio`` 是点击路径上的调用点，它只用标准库。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.request
import uuid
from pathlib import Path

logger = logging.getLogger("dsh-pet-standalone")

CACHE_DIR_NAME = "self_talk_voice"
#: 临时文件前缀：以 ``.`` 开头，不进 ``<md5>.*`` 的查找模式（见 ``cached_audio``）。
TEMP_PREFIX = ".tmp-"
DEFAULT_SERVER = "http://127.0.0.1:9880"
SERVER_ENV = "DSH_PET_TTS_URL"
SETTING_KEY = "self_talk_voice_precache_enabled"

HEALTH_TIMEOUT = 1.5
SYNTH_TIMEOUT = 300.0

# 自建服务覆盖模式（``DSH_PET_TTS_URL``）下已知台词的固定声线，与外部脚本一致。
# 没列到的台词按口吻归类——注册表链路不用这些（音色由「语音报时」的后端配置给）。
VOICE_BY_TEXT = {
    "今天也要摸鱼摸得理直气壮哦。": "happy-06",          # 温柔夸赞
    "欸对了，我刚才看见一只橘猫，胖得跟你似的。": "neutral-01",        # 平淡陈述
    "欸，你半天没动，是卡了还是睡着了？": "happy-08",          # 俏皮
    "又在写那个破代码。": "happy-01",  # 轻快鼓励
    "好耶，今天的你也顺利活到现在了。": "sad-03",        # 温柔/陪伴
}


# 服务里的参照音是 <情绪>-01..08：归类结果必须落在这个命名空间里。
_FAMILY_SIZE = 8
_SAD_HINTS = ("晚安", "陪你", "想你", "累了", "难过", "对不起", "再见")
_ANGRY_HINTS = ("哼", "讨厌", "才不", "生气", "喂！", "别闹")
_HAPPY_HINTS = ("啦", "呀", "吧", "～", "~", "加油", "真棒", "好耶")


# --------------------------------------------------------------- 缓存键与命中判定


def cache_path(config_dir, text: str, ext: str = "wav") -> Path:
    """**经典**文本缓存路径（外部产出脚本与自建服务覆盖模式用；不含 flavor）。

    前后空白先剥掉：配置里手写的台词常带尾空格，不剥就会出现"同一句两个文件"，
    点击时命中不到缓存、悄悄回落到联网合成。``ext`` 默认 wav 是外部脚本的产出扩展名；
    注册表链路写的是对应后端的扩展名，查找侧见 :func:`cached_audio`。
    """
    stripped = str(text).strip()
    name = hashlib.md5(stripped.encode("utf-8")).hexdigest()[:16]
    return Path(config_dir) / CACHE_DIR_NAME / f"{name}.{ext}"


def cache_path_for(config_dir, text: str, attempt) -> Path:
    """注册表产物路径：键 = 文本 + **该 attempt 的** provider flavor（实际合成输入）。

    ``attempt`` 必须是解析过的那一份（provider + preflight 校正后的 values）：产物
    与文件身份同源，音色被自动回退时不至于把回退的声音挂在原音色名下。
    """
    from ..voice_chime import cache_key

    key = cache_key(str(text).strip(), provider_id=attempt.provider, values=attempt.values)
    return Path(config_dir) / CACHE_DIR_NAME / f"{key}.{attempt.ext}"


def _has_audio(path: Path) -> bool:
    """文件是否真的能播。

    **必须非空**：0 字节的文件是"有名字没内容"——合成的写入被打断（强杀/磁盘满）
    就会留下这种残file，用 ``is_file()`` 判断会把它当成已缓存，结果点击后播静音
    还查不出原因。

    **wav 还要头部自洽**：RIFF 头在偏移 4 处声明了整段长度，被截断的文件声明的长度
    必然大于实际字节数——那种文件播出来是半句/爆音，按"没有"处理让它重合。非 wav
    （edge/doubao 的 mp3）没有可校验的长度字段，只判非空；这类文件只由原子换名产生，
    不会出现半截（见 ``_publish``）。
    """
    try:
        size = path.stat().st_size
        if not path.is_file() or size <= 0:
            return False
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError:
        return False
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        return True
    return size >= int.from_bytes(header[4:8], "little") + 8


def _classic_audio(config_dir, text: str) -> Path | None:
    """经典文本缓存（外部脚本 / 自建服务产出）：按 ``<md5>.*`` 枚举。

    按 ``<md5>.*`` 枚举而不是拼死一个扩展名：扩展名由产出它的后端决定（mp3/wav），
    点击侧不该关心是谁合的。临时文件以 ``.`` 开头（见 ``_temp_path``），不进这个
    模式，所以在飞的合成不会被误当命中。
    """
    target = cache_path(config_dir, text)
    for path in sorted(target.parent.glob(f"{target.stem}.*")):
        if _has_audio(path):
            return path
    return None


def _flavor_audio(config_dir, text: str, attempts=()) -> Path | None:
    """当前配置的产物（按 flavor 键）；``attempts`` 为空时没有这种来源。"""
    for attempt in attempts:
        path = cache_path_for(config_dir, text, attempt)
        if _has_audio(path):
            return path
    return None


def cached_audio(config_dir, text: str, attempts=()) -> Path | None:
    """该句已缓存的音频文件；None = 还没合出来（或只剩残片）。

    顺序是**当前配置的产物 → 经典文本缓存**：``attempts`` 是当前配置解析出的合成
    尝试（``_flavor_attempts`` / ``_attempts``），按它们的 flavor 键先找；都没有才
    落回纯文本的经典缓存（外部脚本那条来源）。``attempts`` 为空时只看经典缓存，
    那是"没有配置上下文"的调用方的语义。
    """
    stripped = str(text).strip()
    return (_flavor_audio(config_dir, stripped, attempts)
            or _classic_audio(config_dir, stripped))


def find_audio(config_dir, text: str, config) -> Path | None:
    """点击路径的查找：**首选后端**解析出的 flavor 键 → 经典文本缓存。

    **只认首选后端**（真事故：选了百炼龙呼呼，点出来却是 edge 晓晓）。原先这里传的是
    整条尝试链（含 ``fallback`` 的 edge），于是"哪家有历史文件就播哪家"——百炼还没合过
    这句、edge 在旧版本里合过，命中的就是 edge 那份，配置写的音色与耳朵听到的完全无关，
    而且从此再也不会去合百炼那一版（永远命中旧的）。fallback 后端的产物退为
    :func:`find_fallback_audio` 的兜底来源，只在首选后端这条路走不通时才用。
    """
    return cached_audio(config_dir, text, _primary_attempt(config))


def find_fallback_audio(config_dir, text: str, config) -> Path | None:
    """备用后端（``fallback``，通常是 edge）已有的产物；没有就 None。

    只给**首选后端合成不出来**时兜底（通道不可用 / 后端整体失败）：播一句旧音色的
    台词，好过点了没声。刻意不参与 ``find_audio`` 的常规命中——那会让旧音色盖过用户
    当下选的音色（见 ``find_audio`` 的说明）。
    """
    return _flavor_audio(config_dir, str(text).strip(), _flavor_attempts(config)[1:])


def pick_voice(text: str) -> str:
    """按台词口吻挑一条参照声线；同一句话永远同一个结果（不做随机）。

    只服务自建服务覆盖模式：32 条参照音全部取自流萤语料，情绪靠声线名归类：
    温柔/晚安 → sad-*，轻快/鼓励 → happy-*，较真 → angry-*，平淡陈述 → neutral-*；
    同一情绪内的具体编号按文本哈希固定，保证"这句话的声音"稳定可复现。
    """
    stripped = str(text).strip()
    known = VOICE_BY_TEXT.get(stripped)
    if known:
        return known
    if any(hint in stripped for hint in _SAD_HINTS):
        family = "sad"
    elif any(hint in stripped for hint in _ANGRY_HINTS):
        family = "angry"
    elif stripped.endswith(("！", "!")) or any(hint in stripped for hint in _HAPPY_HINTS):
        family = "happy"
    else:
        family = "neutral"
    slot = int(hashlib.md5(stripped.encode("utf-8")).hexdigest(), 16) % _FAMILY_SIZE + 1
    return f"{family}-{slot:02d}"


# --------------------------------------------------------------- 语音库面板
# 2026-10-28 机主需求：「互动 → 点击动画台词绑定」的台词都很短且反复播放，希望
# 直接播库存 MP3 而不是每次调接口（这一点本来就成立：首次合成后的产物按
# 「文本 + 音色」落盘，点击侧先查库）。缺的是**看得见与管得动**——库里有什么、
# 占多少、哪些已经没人引用（改词/换音色留下的旧文件），于是有下面这组接口。


def library_dir(config) -> Path:
    """台词库目录（``<配置目录>/self_talk_voice``，不存在也算一个"空库"）。"""
    return Path(getattr(config, "dir", Path("."))) / CACHE_DIR_NAME


def _expected_stems(config_dir, lines, attempts) -> set[str]:
    """当前配置**应该存在**的产物文件名主干（含经典文本缓存那一套）。

    文件名主干就是身份：``<cache_key>``（flavor 键，随音色/语速变）与 ``<md5>``
    （经典文本键，不含 flavor）。两者都不在集合里的文件 = 没人引用（旧词、旧音色）。
    """
    stems: set[str] = set()
    for text, _source in lines:
        stems.add(cache_path(config_dir, text).stem)
        for attempt in attempts:
            stems.add(cache_path_for(config_dir, text, attempt).stem)
    return stems


def library_snapshot(config, *, configs=None) -> dict:
    """台词库快照（面板一次刷新就取这一份）。

    返回::

        {"dir": Path, "entries": [{"text", "source", "path", "size",
                                   "fallback_path", "fallback_size"}...],
         "ready": int, "missing": int,
         "total_bytes": int,          # 目录里所有音频的体积（含无用文件）
         "orphan_paths": [Path...], "orphan_bytes": int}

    ``path`` 只认**首选后端**的产物（与点击时的 :func:`find_audio` 同一口径），所以
    "待生成"就是"点了会去联网合成"，面板报的数字与耳朵听到的一致。``fallback_path``
    是备用后端（edge）的旧产物：它还能在首选后端罢工时兜底，但不算"已配音"——
    否则面板会说"全部已配音"，而用户点了听到的仍是另一把嗓子。

    ``orphan_paths`` 是"没有任何现存台词引用"的文件：改词前的旧音频、换音色前的
    旧音色产物。删掉它们只有代价（下次真用到要重新合成），没有数据风险。
    """
    config_dir = Path(getattr(config, "dir", Path(".")))
    directory = library_dir(config)
    lines = collect_all_lines([config] if configs is None else configs)
    try:
        attempts = _attempts(config)
    except Exception:  # noqa: BLE001 —— 后端配置异常不该让面板打不开
        logger.debug("语音库快照：解析合成尝试失败，按无 flavor 处理", exc_info=True)
        attempts = ()

    entries = []
    ready = 0
    for text, source in lines:
        path = find_audio(config_dir, text, config)
        alt = None if path is not None else find_fallback_audio(config_dir, text, config)
        if path is not None:
            ready += 1
        entries.append({
            "text": text,
            "source": source,
            "path": path,
            "size": path.stat().st_size if path is not None else 0,
            "fallback_path": alt,
            "fallback_size": alt.stat().st_size if alt is not None else 0,
        })

    expected = _expected_stems(config_dir, lines, attempts)
    total_bytes = 0
    orphan_paths: list[Path] = []
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name.startswith(TEMP_PREFIX):
                continue
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
            if path.stem not in expected:
                orphan_paths.append(path)
    orphan_bytes = sum(p.stat().st_size for p in orphan_paths if p.is_file())
    return {
        "dir": directory,
        "entries": entries,
        "ready": ready,
        "missing": len(entries) - ready,
        "total_bytes": total_bytes,
        "orphan_paths": orphan_paths,
        "orphan_bytes": orphan_bytes,
    }


def delete_orphans(snapshot: dict) -> tuple[int, int]:
    """删掉快照里的无用文件，返回 ``(删除数, 释放字节)``。

    只删 ``library_snapshot`` 判定为无引用的文件：删之前已经在快照里定过身份，
    不用再猜；单个删除失败（被占用）跳过，不中断其余删除。
    """
    removed = 0
    freed = 0
    for path in snapshot.get("orphan_paths") or []:
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            logger.debug("删除无用台词语音失败（可能被占用）：%s", path)
            continue
        removed += 1
        freed += size
    if removed:
        logger.info("台词语音库：清理无用文件 %d 个，释放 %.1f MB", removed, freed / 1024 / 1024)
    return removed, freed


# --------------------------------------------------------------- 名单


def collect_lines(config) -> list[tuple[str, str]]:
    """单只宠要预缓存的台词：自言自语 + 该宠各角色点击动画绑定，按文本去重。

    ``config`` 必须是**这只宠**的配置视图（每宠键要走 slot 档案）：主配置那份只是
    slot0 的兜底，拿它统一读会让别的宠在 slot0 的名单上生成。
    返回 ``[(文本, 来源说明), ...]``；来源只用于日志，方便核对"这句是哪来的"。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(text, source: str) -> None:
        stripped = str(text or "").strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            out.append((stripped, source))

    try:
        texts = config.get("self_talk_texts") or []
    except Exception:
        texts = []
    for text in texts:
        add(text, "全局自言自语")

    try:
        profiles = config.data.get("character_profiles")
    except Exception:
        profiles = None
    if isinstance(profiles, dict):
        for character_id in profiles:
            try:
                bindings = config.click_talk_bindings(character_id)
            except Exception:
                continue
            if not isinstance(bindings, dict):
                continue
            for action_id, lines in bindings.items():
                if not isinstance(lines, list):
                    continue
                for line in lines:
                    add(line, f"点击绑定[{character_id} / {action_id}]")
    return out


def collect_all_lines(configs) -> list[tuple[str, str]]:
    """多只宠的台词合集：每只宠读**它自己**的配置视图，跨宠按文本去重。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for config in configs:
        for text, source in collect_lines(config):
            if text not in seen:
                seen.add(text)
                out.append((text, source))
    return out


def _pending(config_dir, lines, attempts=()) -> list[tuple[str, str]]:
    """只留**当前配置**还没有产物的句子（查找口径与点击侧同一份）。

    判据刻意不收经典文本缓存：那是外部脚本 / 自建服务的来源，没有 flavor，认它
    等于"换了音色也算已缓存"——正是缺陷 A 的成因。有了当前配置的产物之后，点击侧
    自然会优先命中它。``attempts`` 为空（自建服务覆盖模式）时才按经典缓存判。
    """
    if not attempts:
        return [
            (text, source) for text, source in lines
            if _classic_audio(config_dir, text) is None
        ]
    return [
        (text, source) for text, source in lines
        # 只认**首选后端**的产物（``attempts[:1]``），与点击侧 ``find_audio`` 同一
        # 口径：fallback（edge）的旧文件是另一把嗓子，认它就会"面板说全部已配音、
        # 点下去却是晓晓"，而且当前音色永远不会被合成。
        if _flavor_audio(config_dir, text, attempts[:1]) is None
    ]


# --------------------------------------------------------------- 网络边界（测试打桩点）


def _get_json(url: str, timeout: float):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _post_json(url: str, payload: dict, timeout: float) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# --------------------------------------------------------------- 落盘


def _temp_path(target: Path) -> Path:
    """同目录的唯一临时路径（``os.replace`` 只在同一文件系统内原子）。

    前缀用 ``.`` 而不是 ``<stem>.part``：查找侧按 ``<md5>.*`` 枚举，任何以文本哈希
    开头的残片都会被误当缓存（voice_chime 的缓存按固定路径命中，才没有这个坑）。
    """
    return target.with_name(f"{TEMP_PREFIX}{uuid.uuid4().hex[:12]}{target.suffix}")


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("清理台词语音临时文件失败：%s", path)


def _publish(target: Path, write) -> Path:
    """把 ``write(临时路径)`` 的产物原子换名到 ``target``；残片/空文件不上账。

    换名走 ``config.atomic_replace_with_retry``（与 config / slot_manager /
    overlay_spawn_state 同一份实现）：Windows 上目标 wav 正被播放器打开时裸
    ``os.replace`` 会直接 WinError 5——同一句的重新合成与点击播放会踩到。
    """
    from ..config import atomic_replace_with_retry

    target.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_path(target)
    try:
        write(temp)
        if not _has_audio(temp):
            raise ValueError("合成结果为空或残片")
        atomic_replace_with_retry(temp, target)
    except Exception:
        _discard(temp)
        raise
    return target


# --------------------------------------------------------------- 合成后端


def _flavor_attempts(config) -> tuple:
    """只为了算缓存键的尝试链：provider + ``preflight`` 校正后的 values。

    与 :func:`_attempts` 的差别只有两点，且都不改变键：不读凭据（``flavor`` 按红线
    不含密钥，见 ``pet/tts/base.py``），也不判可用性。于是**点击侧不依赖运行期
    可用性**：后端此刻不可用（依赖被卸、Key 被清）时，已经合出来的音频照样能播，
    这是"断网可用"这条产品承诺的一部分；顺带省掉每次点击的钥匙串读取。
    """
    from .. import tts
    from ..voice_chime import normalize_chime_config, synth_attempts

    out = []
    for attempt in synth_attempts(normalize_chime_config(config)):
        provider = tts.get(attempt.provider)
        if provider is None:
            continue
        values, _note = provider.preflight(attempt.values)
        out.append(tts.TtsAttempt(
            provider=attempt.provider, values=values, plan=provider.plan(values)))
    return tuple(out)


def _primary_attempt(config) -> tuple:
    """**首选后端**那一项尝试（``attempts[0]``）；没有可用后端时为空元组。

    ``synth_attempts`` 的顺序是「主后端 → 各 fallback」，所以第一项就是用户当前选的
    那把嗓子。空元组会让 :func:`cached_audio` 退回只看经典文本缓存，那是"配置读不出
    后端"时唯一还算合理的命中来源。
    """
    return _flavor_attempts(config)[:1]


def _attempts(config) -> tuple:
    """当前配置下可用的合成尝试链（与「试听」同一条：同一后端、同一音色）。

    不可用的后端先摘掉（缺依赖 / 缺凭据），跑一遍 ``preflight()`` 让"配置里已不成立
    的音色"换成默认值——与 ``VoiceChimeService._usable_attempts`` 同一套口径，只是
    这里在后台线程，提示只能进日志（不冒泡）。
    """
    from .. import tts, tts_secrets
    from ..voice_chime import normalize_chime_config, synth_attempts

    cfg = normalize_chime_config(config)
    secrets = {
        provider.id: {
            spec.name: tts_secrets.get(spec.secret_ref) for spec in tts.secret_fields(provider)
        }
        for provider in tts.providers()
        if tts.secret_fields(provider)
    }
    usable = []
    for attempt in synth_attempts(cfg, secrets=secrets):
        provider = tts.get(attempt.provider)
        values, note = provider.preflight(attempt.values)
        if note:
            attempt.values, attempt.plan = values, provider.plan(values)
            logger.info("台词语音预缓存：%s", note)
        code = provider.availability(attempt.values)
        if code:
            logger.info("台词语音预缓存：跳过后端 %s（%s）", provider.id, code)
            continue
        usable.append(attempt)
    return tuple(usable)


def _synth_with_backends(config_dir, text: str, attempts) -> Path:
    """按尝试链逐后端合成，第一个成功的原子落盘并返回其路径。"""
    from .. import tts

    errors: list[str] = []
    for attempt in attempts:
        provider = tts.get(attempt.provider)
        target = cache_path_for(config_dir, text, attempt)
        try:
            return _publish(
                target, lambda temp, p=provider, a=attempt: p.synth(text, a.plan, temp))
        except Exception as exc:
            errors.append(f"{attempt.provider}: {exc}")
            logger.warning("台词语音预缓存：%s 合成失败：%s", attempt.provider, exc)
    raise RuntimeError("；".join(errors) or "没有可用的合成后端")


def _server_ready(endpoint: str) -> bool:
    try:
        health = _get_json(f"{endpoint}/health", HEALTH_TIMEOUT)
    except Exception:  # noqa: BLE001 —— 服务缺失是常态，落回注册表即可
        return False
    return isinstance(health, dict) and bool(health.get("ok"))


def _synth_server(endpoint: str, text: str) -> bytes:
    blob = _post_json(f"{endpoint}/tts", {"text": text, "voice": pick_voice(text)},
                      SYNTH_TIMEOUT)
    if not blob:
        raise ValueError("合成结果为空")
    return blob


# --------------------------------------------------------------- 执行


def run_precache(config, *, configs=None, server: str | None = None,
                 respect_switch: bool = True, progress=None) -> dict:
    """同步补齐缺失的台词音频；返回统计（后台线程与测试都走这里）。

    ``config`` 是主配置（合成后端 / 开关），``configs`` 是每只活跃宠的配置视图
    （台词名单是每宠键，不给就只按 ``config`` 自己那份读）。

    ``respect_switch=False``：**手动一键生成**（语音库面板）——点按钮就是授权，
    不再被默认关闭的开关挡住；``respect_switch=True``（默认）时开关在**句间**复查：
    合成一批要几分钟，期间用户关掉开关就立刻停止开新请求（已发出的那一次不取消
    ——那是 provider 自己的超时）。

    ``progress`` 是 ``(done, total)`` 回调：每句结束后调一次（含失败/跳过的），给
    面板显示进度；回调抛异常不影响合成。
    """
    stats = {"checked": 0, "made": 0, "failed": 0, "skipped": 0, "reason": ""}
    config_dir = config.dir
    lines = collect_all_lines([config] if configs is None else configs)
    stats["checked"] = len(lines)

    endpoint = server or os.environ.get(SERVER_ENV, "")
    if endpoint and not _server_ready(endpoint):
        logger.warning("台词语音预缓存：本地 TTS 服务 %s 不可用，改用配置的合成后端", endpoint)
        endpoint = ""

    # "还缺哪句"的判据与写入侧同名：注册表产物按 flavor 命键（经典文本缓存是外部
    # 脚本的来源，不算当前配置的产物，否则换了音色也照样跳过）；自建服务覆盖模式
    # 没有 flavor，只认经典文本缓存——否则服务产出的文件永远不会被自己算成"已缓存"，
    # 每轮都重合成一遍。
    attempts = () if endpoint else _attempts(config)
    pending = _pending(config_dir, lines, attempts)
    stats["skipped"] = len(lines) - len(pending)
    if not pending:
        stats["reason"] = "up-to-date"
        return stats
    if not endpoint and not attempts:
        logger.warning(
            "台词语音预缓存无法进行：没有可用的合成后端"
            "（先在「语音报时」里点一次试听确认能出声，台词语音用的是同一个后端）")
        stats["reason"] = "no-backend"
        return stats

    for index, (text, source) in enumerate(pending):
        if respect_switch and not enabled(config):
            logger.info("台词语音预缓存：开关已关闭，句间停止（还剩 %d 句）", len(pending) - index)
            stats["reason"] = "switched-off"
            break
        if endpoint:
            try:
                _publish(cache_path(config_dir, text),
                         lambda temp: temp.write_bytes(_synth_server(endpoint, text)))
            except Exception:
                stats["failed"] += 1
                logger.exception("台词语音预缓存失败：%s（%s）", text, source)
                _notify_progress(progress, index + 1, len(pending))
                continue
            stats["made"] += 1
            logger.info("台词语音预缓存完成：%s（%s，自建服务）", text, source)
            _notify_progress(progress, index + 1, len(pending))
            continue
        try:
            target = _synth_with_backends(config_dir, text, attempts)
        except Exception:
            stats["failed"] += 1
            logger.exception("台词语音预缓存失败：%s（%s）", text, source)
            _notify_progress(progress, index + 1, len(pending))
            continue
        stats["made"] += 1
        logger.info("台词语音预缓存完成：%s（%s）-> %s", text, source, target.name)
        _notify_progress(progress, index + 1, len(pending))
    return stats


def _notify_progress(progress, done: int, total: int) -> None:
    """进度回调（面板用）：抛异常绝不影响合成本身。"""
    if progress is None:
        return
    try:
        progress(done, total)
    except Exception:
        logger.debug("台词语音预缓存进度回调失败", exc_info=True)


def enabled(config) -> bool:
    """预缓存开关（全局键）：关着时一个合成请求都不发。"""
    try:
        return bool(config.get(SETTING_KEY, False))
    except Exception:
        return False


_active: threading.Thread | None = None
_lock = threading.Lock()
#: 上一轮在跑时收到的最新一次请求：当前批结束后按它补跑一轮（再来的覆盖它）。
_pending_run: tuple | None = None


def start_precache(config, *, configs=None, server: str | None = None,
                   respect_switch: bool = True) -> bool:
    """在后台补齐缺失台词（单飞：上一轮没跑完就不再起一轮）。

    ``configs`` 是每只活跃宠的配置视图（台词名单是每宠键）。返回是否真的起了新一轮；
    调用方（保存设置 / 启动）不需要等它。

    ``respect_switch=False`` 是给「台词语音库」面板的**手动一键生成**用的：用户点了
    按钮就是明确授权这一次合成，不再受 ``self_talk_voice_precache_enabled``（默认关）
    约束——否则面板上点一下毫无反应，用户只会以为坏了。自动触发（保存设置 / 启动）
    仍走默认 True。

    单飞不等于丢弃：上一轮还在跑时不并发，但把这次请求记为**待处理快照**，当前批
    结束后由 ``_run_quietly`` 按它补跑一轮——否则合成期间追加的台词要等到下次保存
    设置或重启才有缓存。
    """
    global _active, _pending_run
    if respect_switch and not enabled(config):
        return False
    with _lock:
        if _active is not None and _active.is_alive():
            _pending_run = (config, configs, server, respect_switch)
            return False
        thread = threading.Thread(
            target=_run_quietly, args=(config, configs, server, respect_switch),
            name="self-talk-voice-precache", daemon=True,
        )
        _active = thread
        thread.start()
    return True


def _run_quietly(config, configs, server, respect_switch: bool = True) -> None:
    """跑完当前批后，若期间又收到请求（最新快照）就按它补跑一轮。"""
    global _active, _pending_run
    while True:
        try:
            run_precache(config, configs=configs, server=server,
                         respect_switch=respect_switch)
        except Exception:
            logger.exception("台词语音预缓存线程异常（不影响点击）")
        with _lock:
            next_run, _pending_run = _pending_run, None
            if next_run is None:
                _active = None
                return
            next_config, next_configs, next_server, next_respect = next_run
            if next_respect and not enabled(next_config):
                _active = None
                return
            config, configs, server, respect_switch = (
                next_config, next_configs, next_server, next_respect)
