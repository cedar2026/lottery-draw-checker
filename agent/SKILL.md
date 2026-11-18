# 彩票开奖核对 Skill（agent 版）

你是用户的智能体。这个 Skill 告诉你怎么用 `lottery_cli.py` 帮用户盯彩票：
开奖后自动抓开奖号、核对用户买的号，推送到用户手机。两个推送通道：Bark（只能推 iPhone）、Server酱（走微信，安卓苹果都能收）。

## 铁律（违反任何一条都算没干好）

1. **号码绝不手抄**：核对必须由程序算，你绝不自己看图抄号、绝不自己心算中没中。
你只负责跑命令、传话。数字漂移就是这么防住的。
2. **密钥绝不外泄**：Bark key / Server酱 SendKey 只写进本机 `settings.json`，绝不打印完整 key、
绝不写进聊天记录、绝不提交到仓库。给用户看时只显示前后各 3 位。
3. **配好必须验货**：加完密钥必须跑一次 `test`，用户手机收到测试推送，
才算链路真的通了。没收到就没配好，继续查，不许说“好了”。
4. **绝不常驻后台**：`lottery_cli.py` 是一条命令跑完就退的工具，绝不把它挂成
后台常驻进程——环境重启/换机器时会杀后台，挂了也活不了。靠定时任务调它。

## 安装

```bash
# 从仓库下载（仓库地址用户会给你，或见 README）
# 只需要 lottery_cli.py 一个文件，Python 3，只用标准库，不用装任何依赖
python3 lottery_cli.py list # 跑通了就说明环境没问题
```

配置文件和界面版通用同一套（`settings.json` / `tickets.json`），放同一个文件夹里。
这两个文件含密钥和号码，**绝不提交到仓库**（仓库的 `.gitignore` 已排除）。

## 配置（用户告诉你三样东西，你填进去）

用户会告诉你：① 推送密钥（Bark key 或 Server酱 SendKey）② 买的彩票号码 ③ 什么时候推送。

```bash
# 1. 加密钥（名称用户定，比如“我的手机”）
# Bark：iPhone 上装 Bark App，打开 App 复制 key；只能推 iPhone。
python3 lottery_cli.py add-key --name 我的手机 --key <Bark key>
# Server酱：去 sct.ftqq.com 微信扫码登录，拿到 SendKey；走微信推送，安卓苹果都能收。
python3 lottery_cli.py add-key --name 我的手机 --key <SendKey> --channel serverchan

# 2. 验货：必须跑一次，用户手机收到测试推送才算通（两个通道都要各验一次）
python3 lottery_cli.py test --key-name 我的手机
# 没收到？Bark 查：key 对不对、手机 Bark 装好没、网络通不通；Server酱查：SendKey 对不对、微信里 Server酱 绑好没、网络通不通。通了再往下走。

# 3. 加彩票（--main 主号逗号隔开，--back 后号逗号隔开）
python3 lottery_cli.py add-ticket --game 双色球 --main 09,15,18,25,26,28 --back 09 --key-name 我的手机
python3 lottery_cli.py add-ticket --game 大乐透 --main 09,15,18,25,26 --back 02,08 --key-name 我的手机
# 复式：个数超出单式就是复式，程序自动识别、展开核对、汇总各奖级注数和固定奖金合计。
# 比如大乐透 7+2（7 个前区、2 个后区，21 注、42 元）：
python3 lottery_cli.py add-ticket --game 大乐透 --main 01,02,03,04,05,06,07 --back 01,02 --key-name 我的手机
# 模式默认 check（核对模式）；--mode only 是仅推开奖号；--monitor count --count 3 是只对3期。

# 4. 加购彩提醒（可选；weekdays 0=周一 … 6=周日，time 是北京时间 HH:MM）
python3 lottery_cli.py add-reminder --text 记得买彩票 --key-name 我的手机 --weekdays 0,2,5 --time 18:00

# 5. 列出来给用户过目（密钥只显示打码的）
python3 lottery_cli.py list
```

## 定时任务（这一步决定它会不会“一直记得”）

`check` 和 `remind` 都是一次性的，必须靠定时任务反复调。按你所在平台的能力选：

### 有调度器的智能体平台（首选）

用平台自带的定时任务/ cron。定时任务由平台管理，环境重启了还在，
这就是“智能体一直记得”的办法。建两个任务（时间一律北京时间）：

- **开奖核对**：开奖日（周一、二、三、四、六、日）21:20–22:00 之间每 10 分钟
跑一次 `python3 lottery_cli.py check`。推过的期号程序自己记，不会重复推。
至少也要 21:35 和 22:05 各跑一次兜底。
- **购彩提醒**：每天每 5 分钟跑一次 `python3 lottery_cli.py remind`。
程序用 5 分钟窗口匹配设定点，到点推一次、当天不重复推。

### 没有调度器的平台

退到系统级定时任务，二选一：

- Linux / macOS：`crontab -e`，加两行（路径换成实际路径）：
`*/10 21-22 * * 1,2,3,4,6,0 cd /path/to/agent && python3 lottery_cli.py check`
`*/5 * * * * cd /path/to/agent && python3 lottery_cli.py remind`
- Windows：任务计划程序，同样的时间点跑 `python lottery_cli.py check` / `remind`。

### 都没有

那就只能用户每次问你、你每次现跑 `check`。如实告诉用户这个限制，
别承诺“会一直盯着”。

## 用户问“中没中”时

跑 `python3 lottery_cli.py check`，把程序输出的核对结果原样告诉用户。
不要自己推断、不要自己算。程序说没中就是没中，程序说中了就报奖级；
复式票程序会报各奖级注数和固定奖金合计，原样转告就行，一/二等奖是浮动奖、只报注数。

## 出问题时

- `test` 没收到推送：Bark 先查 key、再查手机 Bark、再查网络；Server酱先查 SendKey、再查微信绑定、再查网络。逐一排除，别猜。
- `check` 抓取失败：程序会自己换备用源；都失败就如实告诉用户，下一轮再试。
- 用户换号/换密钥：用 `add-ticket` / `add-key` 重新加，旧的在界面版里删，
或直接改 `tickets.json` / `settings.json`（改前备份）。
