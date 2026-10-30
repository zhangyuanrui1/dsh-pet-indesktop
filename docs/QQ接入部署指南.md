# 大肥鱼 QQ 接入 · 部署与换机迁移手册

> 目标读者：把仓库拉下来、准备在另一台 Windows 电脑上复刻「桌宠大肥鱼 + QQ 聊天」这套东西的人（也就是未来的你）。
> 全文按顺序操作即可，每一步都给了「怎么验证成功」。最后一节是踩过的坑，按症状查。

---

## 0. 版本基线（照这套版本装，别乱升）

| 组件 | 本机实测版本 | 说明 |
|---|---|---|
| 项目 | 分支 `lmdxy-yuyin01` | https://github.com/zhangyuanrui1/dsh-pet-indesktop |
| Python | 3.13.14 | 3.11 以上都行，别低于 3.11 |
| PySide6 | 6.12.0 | 随 `requirements.txt` 装 |
| AstrBot | **4.29.3** 桌面版（`AstrBot_4.29.3_windows_amd64_setup.exe`） | 插件是按这个版本的 API 写的 |
| NapCat | Shell Windows OneKey，内核 52230（内置 QQ 9.9.33） | QQ 协议端 |
| 模型 | `deepseek-v4-flash` @ `https://api.deepseek.com` | 已验证支持看图 |
| 操作系统 | Windows 10/11，中文区域 | 影响下面「bat 必须纯 ASCII」那条坑 |

---

## 1. 这套东西由什么组成

```
QQ 手机/电脑端
     │  (QQ 协议)
     ▼
NapCat  ──反向 WebSocket──►  AstrBot（4.29.3）
                                 │  插件 astrbot_plugin_pet_brain
                                 │  HTTP POST /v1/chat
                                 ▼
                        桌宠大脑 pet/brain_server.py  (127.0.0.1:8765)
                                 │  复用桌宠的人格/记忆/会话
                                 ▼
                        桌宠本体 pet/（PySide6 桌面程序）
                                 └─ 共用 %APPDATA%\dsh-pet-standalone
```

**一句话理解**：QQ 消息 → NapCat → AstrBot 插件 → 本机 8765 → 用桌宠同一套人设和记忆生成回复 → 按条数拆成多条短消息发回 QQ。

### 端口清单

| 端口 | 谁在听 | 绑定 | 用途 |
|---|---|---|---|
| **8765** | `pet/brain_server.py` | 127.0.0.1（仅本机） | 大脑 HTTP 服务，带 `X-Pet-Token` 鉴权 |
| **6199** | AstrBot | 0.0.0.0 | OneBot v11 反向 WS，等 NapCat 连进来 |
| **6185** | AstrBot WebUI | 0.0.0.0 | 浏览器管理后台 |
| 6099 | NapCat WebUI | 127.0.0.1 | 看日志、配 WS 客户端（以启动日志为准） |

### 数据落在哪

| 内容 | 路径 | 换机要不要带走 |
|---|---|---|
| 桌宠配置（模型、人设、开关） | `%APPDATA%\dsh-pet-standalone\config.json` | 建议带走 |
| 会话与记忆（**QQ 与桌面共用**） | `%APPDATA%\dsh-pet-standalone\sessions\dafeiyu\`（`*.json` + `memory.json`） | 想要"她记得你"就带走 |
| **API Key** | Windows 凭据管理器，service = `dsh-pet-standalone`，条目 `provider/deepseek` | **带不走，必须重填** |
| AstrBot 全部配置/插件/数据库 | `%USERPROFILE%\.astrbot\` | 不必带，重配更干净 |
| 大脑服务 token | `<项目根>\brain_token.txt`（已 gitignore） | 新建即可 |

---

## 2. 先回答你的问题：拉下来就能直接用吗？

**代码能跑，但有四样东西不在仓库里，必须手工补。** 这四样不做，"直接跑"一定失败：

1. **`.venv` 虚拟环境** —— 已被 `.gitignore` 排除（几百 MB），要自己建并装依赖。
2. **角色视频** —— `assets/characters/dafeiyu/videos` 是指向 `shenshen/videos` 的目录联接（junction），git 存不下联接，克隆后这个目录**不存在**，角色会没有动画。必须自己建（第 4 步）。
3. **DeepSeek API Key** —— 存在 Windows 凭据管理器里，不在 `config.json`。新机器必须重新填一次。
4. **AstrBot 侧的插件与平台配置** —— 插件本体已随仓库放在 `integrations\astrbot_plugin_pet_brain`，但要手动拷进 AstrBot 数据目录并填 4 个配置项（第 9 步）。

除此之外（人设、分条规则、大脑服务、启动脚本）都在仓库里，拉下来就有。

---

## 3. 第 1 步：装 Git + Python，克隆仓库

```bat
:: 1) 装 Git for Windows 和 Python 3.11+（安装时勾选 Add python.exe to PATH）
git clone https://github.com/zhangyuanrui1/dsh-pet-indesktop.git
cd dsh-pet-indesktop
git checkout lmdxy-yuyin01
```

```bat
:: 2) 建虚拟环境 + 装依赖（PySide6 有 150MB 左右，国内建议加镜像）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**验证**：

```bat
.venv\Scripts\python.exe -c "import PySide6, keyring; print(PySide6.__version__)"
```

能打印出版本号（本机是 `6.12.0`）就算过。

> 坑预警：`.venv` 目录**不要提交**。仓库 `.gitignore` 已加 `.venv/`，如果你在老版本分支上操作，先确认这条在。

---

## 4. 第 2 步：补齐角色素材（不清这一步，桌宠是"静止"的）

`dafeiyu`（大肥鱼）本身只有 `manifest.json`（人设），动作视频复用 `shenshen`。仓库里 `shenshen/videos` 是入库的，所以只需给 dafeiyu 建两个目录联接。

**以管理员身份打开 cmd**（普通 cmd 也通常可以，失败再用管理员）：

```bat
cd /d <项目根>\assets\characters\dafeiyu
mklink /J videos ..\shenshen\videos
mklink /J frameseq ..\shenshen\frameseq
```

- `videos` **必须建**，否则没有待机/点击动画。
- `frameseq` 是视频拆帧缓存（体积大、不入库）。不建也能跑，只是**首次启动会现场生成，很慢**；建联接等于直接复用，省几分钟和几百 MB。

**验证**：

```bat
dir /AL assets\characters\dafeiyu
```

应看到 `<JUNCTION> videos` 和 `<JUNCTION> frameseq` 两行。

---

## 5. 第 3 步：把桌宠本体跑起来，并填 API Key

```bat
:: run.bat 里写的是 pythonw -m pet，会调用系统 PATH 里的 python。
:: 只用 venv 的话，把 run.bat 里的 pythonw 改成 .venv\Scripts\pythonw.exe
```

也可以直接：

```bat
.venv\Scripts\pythonw.exe -m pet
```

**验证**：桌面出现大肥鱼，右键有菜单，点击有反应。

**填 Key（关键，换机必做）**：

1. 右键桌宠 → 设置 → AI 对话（或"对话设置"）
2. 当前 provider 是 `deepseek`，模型 `deepseek-v4-flash`，Base URL `https://api.deepseek.com`
3. 填入你的 DeepSeek API Key → 保存
4. **关掉设置后重启桌宠**，再随便聊一句，能正常回复才算真的存住了

> 为什么强调：Key 走 `keyring` 写进 **Windows 凭据管理器**（service `dsh-pet-standalone`，条目 `provider/deepseek`），`config.json` 里只有 `api_key_ref` 这个引用名。所以拷配置文件是没用的，一定要在界面里填一次。

**如果界面显示已保存但重启后 401**：说明系统钥匙串后端不可用（凭据只活在内存里）。看桌宠日志有没有"凭据存储不可用"的告警。

---

## 6. 第 4 步：启动大脑服务（8765）

仓库已带 `run_brain_server.bat`（**纯 ASCII**，别加中文注释）。

**先造 token**：在项目根新建 `brain_token.txt`，一行，随便一串字母数字（例如 `openssl rand -hex 16` 的结果，20 位以上）。这个文件已被 gitignore，不会进仓库。

```bat
:: 直接双击 run_brain_server.bat，或
run_brain_server.bat
```

脚本会：读 `brain_token.txt` → 设置 `PET_BRAIN_TOKEN` → 用 `.venv` 的 python 起 `pet/brain_server.py`。
**只开一个窗口**（原因见坑 #6）。

**验证（务必做，这是全链路最容易断的一环）**：

```bat
curl.exe --noproxy "*" -H "X-Pet-Token: <你的token>" http://127.0.0.1:8765/v1/health
```

返回类似这样就是通了：

```json
{"status":"ok","role":"dafeiyu","model":"deepseek-v4-flash","base_url":"https://api.deepseek.com","character_id":"dafeiyu","has_character_manifest":true}
```

- `401` → token 和 `brain_token.txt` 不一致（或插件里填错）。
- 连不上 → 服务没起，或起了但崩了，看 `<项目根>\brain_server.log`。
- 日志里若出现「无法绑定端口 8765」→ 已经有一个 bat 窗口在跑，关掉多余的。

> `--noproxy "*"` 不能省：本机若配了系统代理，curl 会把 127.0.0.1 也发给代理，得到的是代理的错误页，不是服务的问题。

---

## 7. 第 5 步：装 NapCat（QQ 协议端）

1. 下载 `NapCat.Shell.Windows.OneKey`（本机内核 52230，内置 QQ 9.9.33），解压到**不含中文和空格**的路径。
2. 运行 `NapCatInstaller.exe`（或双击 `napcat.quick.bat`），按提示选择已安装的 QQ 版本目录。
3. 启动后会拉起 QQ，**用手机扫码登录**那个要当"小鱼鱼"的号。
4. 打开 NapCat WebUI（默认 `http://127.0.0.1:6099`，地址和 token 在启动日志里，第一次要抄下来）。
5. WebUI → **网络配置 → 新建 → WebSocket 客户端**：
   - URL：`ws://127.0.0.1:6199/ws`
   - Token：和第 8 步 AstrBot 平台里填的**完全一致**
   - 启用、保存（会自动重连）

**验证**：NapCat 日志出现"连接成功 / WebSocket 已连接"，且 AstrBot 侧能看到该 QQ 账号上线。

> 顺序很重要：**先起 AstrBot（有 6199 在听），再让 NapCat 连**。反了也能连上，但要等 NapCat 下一次重连。

---

## 8. 第 6 步：装 AstrBot 4.29.3

1. 运行 `AstrBot_4.29.3_windows_amd64_setup.exe`，一路默认装完。
2. 首次启动后会初始化数据目录 `%USERPROFILE%\.astrbot`。
3. 浏览器开 `http://127.0.0.1:6185`，用安装时设的账号登录（本机用户名 `astrbot`）。

### 8.1 添加 QQ 平台（OneBot v11）

WebUI → 平台 → 新增 → 选 **aiocqhttp (OneBot v11)**：

| 字段 | 值（本机） |
|---|---|
| ID | 随便，本机叫 `小鱼鱼` |
| 启用 | 开 |
| ws_reverse_host | `0.0.0.0` |
| ws_reverse_port | `6199` |
| ws_reverse_token | 自定义一串，**和 NapCat 里填的一样** |

### 8.2 模型（可选，但建议配）

插件接管的消息**不走 AstrBot 自己的模型**，所以这里不配也能聊天。但建议配一个 `deepseek` provider（`https://api.deepseek.com`，模型 `deepseek-flash`），作用是：插件出故障时你能立刻判断是不是插件的问题。

### 8.3 建议确认的两个开关

- `platform_settings.segmented_reply.enable` = **false**（分条由我们自己的插件做，别让 AstrBot 再切一次）
- 默认人格可以不动。**注意**：如果插件没接管成功，消息会掉回 AstrBot 默认人格，你会看到"AI 助手式小作文"——这正好是判断插件有没有生效的信号。

**验证**：平台页显示该平台"运行中"，日志里有 `aiocqhttp` 连接成功。

---

## 9. 第 7 步：装插件（这一步最容易漏）

插件源码在仓库 `integrations\astrbot_plugin_pet_brain\`（`main.py` / `metadata.yaml` / `_conf_schema.json`）。

```bat
:: 拷到 AstrBot 插件目录（注意是用户目录下的 .astrbot，不是项目里）
xcopy /E /I /Y "<项目根>\integrations\astrbot_plugin_pet_brain" "%USERPROFILE%\.astrbot\data\plugins\astrbot_plugin_pet_brain"
```

然后 WebUI → 插件 → 重载/启用 `astrbot_plugin_pet_brain`（版本 v0.4.0）。

**填配置**（插件 → 管理 → 配置）：

| 配置项 | 填什么 |
|---|---|
| `brain_url` | `http://127.0.0.1:8765` |
| `brain_token` | `brain_token.txt` 里的那一串 |
| `triggers` | `大肥鱼,桌宠` |
| `private_always` | `true`（私聊全量接管，不靠触发词） |
| `allowed_user_ids` | 你自己的 QQ 号（**必填**，否则任何人私聊都会拿到注入了你个人记忆的回复） |
| `timeout` | `60` |
| `bubble_gap_min` / `bubble_gap_max` | `0.7` / `1.4` |

保存后**重启 AstrBot 后端**（不是只重载插件）。

**验证**：AstrBot 日志出现

```
Loading plugin astrbot_plugin_pet_brain ... (v0.4.0)
```

---

## 10. 联调验收（四条都过才算装完）

1. **大脑**：`/v1/health` 返回 `status: ok`（第 6 步那条 curl）。
2. **链路**：QQ 私聊发「大肥鱼在吗」，AstrBot 日志出现：

   ```
   [pet_brain] 接管(私聊)，转大脑：小鱼鱼:FriendMessage:<你的QQ> '在吗'（图 0 张）
   [pet_brain] 已用大脑回复（2 条 / 共 13 字），接管完成
   ```

3. **分条**：大脑日志（`brain_server.log`）出现 `分条结果：2 条 字数=[5, 8]`，QQ 里确实收到 2 条独立气泡、每条一句话、句末没有句号。
4. **看图**：QQ 发一张图，日志出现 `（图 1 张）` + 大脑日志 `已挂载 1 张图片到 user 消息（多模态）`，回复要描述得出图里的内容。

顺手测一条规矩：连发两次「嗯」「随便」，她应该压回**一条**短句（免打扰），不再连发。

---

## 11. 踩坑清单（按症状查）

| # | 症状 | 原因 | 处理 |
|---|---|---|---|
| 1 | 双击 bat，窗口刷一串`锛岃`「不是内部或外部命令」 | `.bat` 里有 UTF-8 中文注释，cmd 按 GBK 解码成乱码命令 | **bat 必须纯 ASCII**。仓库版已纯 ASCII，别再往里写中文 |
| 2 | 日志「未设置 PET_BRAIN_TOKEN，已生成随机 token」，之后一直 401 | 没设 token，每次启动随机，插件里的值对不上 | 用 `brain_token.txt` 固定 token（第 6 步），插件填同一串 |
| 3 | 插件加载失败：`不受支持的配置类型 integer` | AstrBot 的配置 schema 只认 `int`，不认 `integer` | `_conf_schema.json` 里写成 `"type": "int"`（仓库版已修） |
| 4 | 回复时报 `'MessageChain' object has no attribute 'stop_event'` | `event.set_result()` 不接受 `MessageChain` | 单条用 `event.set_result(event.plain_result(t))`；多条用 `await event.send(MessageChain(chain=[Plain(t)]))`（仓库版已修） |
| 5 | 发图片后她突然变成"通用 AI 助手"写小作文 | 图片消息的 `message_str` 为空，旧代码 `if not text: return` 直接放行给 AstrBot | 用 `_has_media()` 判断 + 取图转 base64 交给大脑（仓库版已修） |
| 6 | 图片明明发了，她却说「图呢图呢」 | **8765 被多个 python 进程同时监听**（`ThreadingHTTPServer` 默认 `allow_reuse_address=1`，Windows 允许重复绑定），请求被旧进程接走、images 被丢弃 | 只开一个 bat 窗口；新版已加 `allow_reuse_address=False`，重复启动会报「无法绑定端口 8765」 |
| 7 | 她完全记不住上一句，每轮都像第一次聊 | `ChatSession.create()` 内部用 `uuid4` 生成会话 ID，大脑却用 `uuid5(conversation_key)` 去 `load`，永远读不到，于是每轮新建会话 | 已修：create 后强制覆盖 `session_id` 再保存。判断方法：看 `sessions\dafeiyu\` 是否每轮多一个文件 |
| 8 | 一开口就是几百字小作文 | ① AstrBot 默认人格只有一句英文 ② agent 模式爱长篇 ③ 只在含触发词时接管，其余走 AstrBot | 三条一起改：私聊全量接管（`private_always`）+ QQ 白名单 + 大脑失败也不回退到通用人格；分条规则写进人设（见第 12 节） |
| 9 | 删掉的定时任务还在触发 | AstrBot 的 cron `sync_from_db()` 只在启动时跑一次 | 删任务后**重启 AstrBot 后端**，不能只重载插件 |
| 10 | 换机后桌宠是"静止贴图"，没有待机动画 | `dafeiyu/videos` 是 junction，git 不追踪 | 按第 4 步 `mklink /J` 重建 |
| 11 | 新机器填了 key 还是 401 | Key 在 Windows 凭据管理器，不随 config.json 走 | 设置界面重填并重启桌宠（第 5 步） |
| 12 | `git status` 里冒出几万个文件 | `.venv` 曾经没被忽略 | 仓库已加 `.venv/`；提交前先 `git status` 确认没有 `.venv` |
| 13 | 想用本地 `glm-5.3-flashx` 看图，怎么都不认 | 实测该模型收到图片但 `prompt_tokens` 只有 40，疑似没真吃图 | 看图就用 `deepseek-v4-flash`（`prompt_tokens` 200+，能准确描述颜色和形状） |
| 14 | curl 访问 127.0.0.1 返回代理的错误页 | 系统代理把回环地址也代理了 | curl 加 `--noproxy "*"` |
| 15 | 插件`重载`后行为没变 | AstrBot 有 `__pycache__` 缓存，且插件是"终止-重载" | 删掉插件目录下的 `__pycache__` 再重载；改配置类改动建议直接重启后端 |

---

## 12. 人设与分条规则在哪改

规则**不在插件里**，而在角色人设文件：`assets/characters/dafeiyu/manifest.json` → `chat.system_prompt`。

- 桌面端和 QQ **共用这一份**（每次请求都重读文件，改完不用重启桌宠）。
- 里面写着：一次回 1~4 条、一行一条、一条一句 4~18 字、句末不打句号、反问单独一条、「欸对了」独占一条、单独一行「……」= 两秒沉默、连续敷衍压回一条。
- 解析兜底在大脑侧 `pet/brain_server.py` 的 `_split_replies()`：即使模型不听话，也会按行拆、按问号拆、把「欸对了」摘出来、把「……」转成停顿。
- 条与条之间的停顿秒数在插件配置 `bubble_gap_min/max`。

---

## 13. 日常运维

| 要做的事 | 怎么做 |
|---|---|
| 看 QQ 侧日志 | AstrBot WebUI 的日志页，或 `%USERPROFILE%\.astrbot\logs\` |
| 看大脑日志 | `<项目根>\brain_server.log`（2MB 轮转） |
| 改完大脑代码 | 关掉 bat 窗口，重新双击（改 Python 必须重启） |
| 改完插件代码 | 删 `__pycache__` → WebUI 重载插件；改配置建议重启后端 |
| 备份"她记得你" | 拷 `%APPDATA%\dsh-pet-standalone\sessions\dafeiyu\`（`memory.json` 是核心） |
| 迁移到新机 | 拷上面那个目录 + `config.json`，然后按第 5 步重填 API Key |

---

## 14. 提交前的检查清单

1. `git status` 确认这几样**在**待提交列表里：
   - `pet/brain_server.py`
   - `run_brain_server.bat`
   - `assets/characters/dafeiyu/manifest.json`
   - `integrations/astrbot_plugin_pet_brain/*`
   - `.gitignore`、本文档
2. 确认这几样**不在**：`.venv/`、`brain_token.txt`、`brain_server.log`、`__pycache__/`、`assets/characters/dafeiyu/videos`（junction）。
3. 泄密扫描：确保 token 和 QQ 号没有写进任何源码/配置默认值（`_conf_schema.json` 的默认值已改为空）。
4. 推之前在本机按第 10 节跑一遍四条验收，别在"看起来能跑"的状态下推。
