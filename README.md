# OneMail 取件服务

运行在 Mail-in-a-Box(MIAB)邮件服务器上的轻量取件服务:管理员为任意邮箱生成一条**免登录、只读**的取件链接,把链接发给需要收验证码的人,对方打开链接即可看到该邮箱的收件箱——**验证码被自动提取并大字展示**,页面每 5 秒自动检查新邮件。

典型场景:团队/家人共用一个注册邮箱收验证码,但不想把邮箱密码交出去。

## 功能列表

- **客户端取件页**(`/m/<token>`,免登录)
  - 「验证码」Tab:只显示提取到验证码的邮件,验证码大字 + 一键复制(带"已复制 ✓"反馈)
  - 「全部邮件」Tab:完整收件箱,分页每页 20 封,最新在前
  - 自动更新:每 5 秒轮询轻量 check 接口,发现新邮件才整页刷新(不丢滚动位置、不刷爆访问日志)
  - 验证码提取:关键词邻近规则(验证码/校验码/动态码/动态密码/安全码/代码/口令,code / verification code / security code / confirmation code / authentication code / OTP / passcode / PIN 等),防年份/长订单号误抓,HTML 邮件先剥标签
  - 邮件详情:超大验证码 hero 区、HTML 正文 iframe sandbox 渲染(高度自适应)、**远端图片默认剥离**,点开关才加载、附件下载
  - 链接失效(过期/撤销/不存在)显示对应的中文提示页,支持配置邮箱地址打码
- **管理端**(`/admin/`,MIAB 管理员邮箱+密码登录)
  - 邮箱卡片(仅显示有链接的邮箱)+ 实时搜索 + 新增链接(可选 1 天/7 天/30 天/永久)
  - 行内操作:复制 URL、访问日志、重置(换 token)、撤销/启用、彻底删除、一键清除已撤销
  - 每条链接的访问日志(时间/IP/UA,Asia/Shanghai 时区)

## 与同类项目的对比

| | OneMail(本项目) | [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) | [AuthInbox](https://github.com/TooonyChen/AuthInbox) |
|---|---|---|---|
| 运行位置 | 自己的邮件服务器(MIAB) | Cloudflare Workers | Cloudflare Workers |
| 面向对象 | **已有真实邮箱**的只读分享 | 一次性临时邮箱地址 | 临时接码邮箱 |
| 依赖 | 无第三方云依赖,数据完全自控 | Cloudflare Email Routing | Cloudflare Email Routing |
| 验证码提取 | ✅ 关键词规则,大字展示+一键复制 | ❌ | ✅ |
| 链接有效期/撤销/访问审计(IP/UA/次数) | ✅ | ❌ | ❌ |
| 账号体系 | 直接复用 MIAB 管理员账号,零额外用户系统 | 自建用户系统 + OAuth/Passkey | 自建 |

选型建议:想要零成本、一次性的临时邮箱,选 cloudflare_temp_email;想把自己服务器上的**真实邮箱**安全地"借"给别人收验证码,选本项目。

## 技术栈

Python 3.10+ / Flask 3 + gunicorn + sqlite3,纯 CSS + vanilla JS(无任何前端框架),只读解析 Maildir(`mailbox` 风格的 cur/new 目录直读)。界面截图可放在 `docs/` 下(本仓库未包含)。

## 目录结构

```
one-mail/
├── app.py                  # 全部后端逻辑(单文件 Flask app)
├── templates/              # Jinja2 模板
│   ├── base.html           # 设计令牌(CSS 变量)+ 全站样式 + 复制 JS
│   ├── inbox.html          # 客户端收件箱(双 Tab + 轮询)
│   ├── message.html        # 邮件详情(hero 验证码 + iframe 正文)
│   ├── invalid.html        # 链接失效提示页(过期/撤销/不存在)
│   ├── login.html          # 管理员登录
│   ├── admin.html          # 管理端首页(邮箱卡片 + 搜索 + 新增)
│   ├── links.html          # 链接汇总
│   └── link_detail.html    # 单链接访问日志
├── deploy/
│   ├── onemail.service     # systemd 单元(含沙箱加固)
│   ├── onemail.nginx.conf  # nginx 反代(先 80,certbot 自动加 443)
│   ├── fail2ban-filter.conf
│   └── fail2ban-jail.conf
├── requirements.txt
├── LICENSE                 # MIT
└── README.md
```

运行时会自动生成(不进版本库,见 .gitignore):`onemail.db`(sqlite)、`secret_key`、`security.log`、`venv/`。

## 部署步骤(从零)

前提:一台已装好 MIAB 的 Ubuntu 服务器(MIAB 提供邮箱、Maildir 和管理 API)。本服务只新增文件,**不改任何 MIAB 管理的配置**。

```bash
# 1. 代码与依赖
mkdir -p /opt/onemail && cp -r app.py templates /opt/onemail/
apt-get install -y python3.10-venv
python3 -m venv /opt/onemail/venv
/opt/onemail/venv/bin/pip install -r requirements.txt

# 2. systemd
cp deploy/onemail.service /etc/systemd/system/onemail.service
systemctl daemon-reload && systemctl enable --now onemail

# 3. nginx(MIAB 的 nginx 已在跑,只新增独立 conf)
cp deploy/onemail.nginx.conf /etc/nginx/conf.d/onemail.conf
# 把 server_name 改成你的域名,DNS A 记录先指好
nginx -t && systemctl reload nginx

# 4. HTTPS
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d <你的域名> --non-interactive --agree-tos -m <管理员邮箱> --redirect

# 5. fail2ban(可选,防爆破联动;MIAB 已装 fail2ban)
cp deploy/fail2ban-filter.conf /etc/fail2ban/filter.d/onemail.conf
cp deploy/fail2ban-jail.conf   /etc/fail2ban/jail.d/onemail.conf
touch /opt/onemail/security.log
fail2ban-client reload
```

与 MIAB 的关系:登录验证和邮箱列表来自 MIAB 管理 API(HTTP Basic Auth 打 `https://127.0.0.1/admin/mail/users?format=json`);邮件正文直接**只读** Maildir 文件,不经过 dovecot,不做任何写操作。

## 配置项(app.py 顶部常量)

| 常量 | 默认 | 说明 |
|---|---|---|
| `MAILBOX_ROOT` | `/home/user-data/mail/mailboxes` | MIAB Maildir 根目录 |
| `MIAB_API` | `https://127.0.0.1/admin/mail/users?format=json` | MIAB 管理 API |
| `MASKED_MAILBOXES` | `{"masked@example.com"}` | 失效提示页上打码显示的邮箱集合(如 `ma**@example.com`);链接有效时始终显示完整地址 |
| `PER_PAGE` | `20` | 收件箱每页封数 |
| `TTL_CHOICES` | 1/7/30 天、永久 | 生成/重置链接时的有效期选项 |
| `LOG_THROTTLE_SEC` | `60` | 同一链接列表页访问日志节流秒数 |
| `LOGIN_FAIL_*` / `TOKEN404_*` | 5 次/10 分钟锁 15 分钟;20 次/分钟 → 429 | 登录防爆破、token 防枚举 |
| `DEV_HOST` / `DEV_PORT` | `127.0.0.1:8200` | 仅 `python app.py` 调试用;生产监听地址在 `deploy/onemail.service` 的 gunicorn `-b` 参数里 |

## 安全机制

- **登录防爆破**:按 `IP+账号` 计失败次数(sqlite),10 分钟内失败 5 次锁 15 分钟,错误文案统一模糊;失败/锁定写 `security.log`
- **token 防枚举**:`/m/` 下无效 token 按 IP 计数,1 分钟超 20 次返回 429;check 轮询接口豁免
- **会话**:MIAB 密码存服务端 sqlite(sessions 表,12 小时过期),cookie 只有随机 sid;cookie Secure + HttpOnly + SameSite=Lax
- **CSRF**:管理端所有 POST 校验 session 绑定的 csrf token
- **安全响应头**:nosniff / X-Frame-Options DENY / Referrer-Policy no-referrer / CSP(含 frame-ancestors 'none')/ HSTS(仅 HTTPS)
- **fail2ban**:独立 jail 匹配 `security.log` 的 LOGIN_FAIL / LOGIN_LOCKED / TOKEN404 / TOKEN429 行,maxretry 10 封 30 分钟(iptables http,https)
- **进程沙箱**:systemd NoNewPrivileges / ProtectSystem=full / ProtectHome=read-only 等(maildir 目录是 MIAB 管的 `drwx------ mail:mail`,非 root 进不去,故保持 root + 沙箱)
- **邮件渲染**:HTML 正文 iframe sandbox(无脚本)+ 远端资源默认剥离;附件文件名 RFC5987 编码;Maildir key 白名单校验防路径穿越

## MIAB 升级注意事项

- 依赖 MIAB 内部 API 的路径和 JSON 结构(按域名分组、每组 `users` 数组);升级后若管理端登录 401/500,先查这个接口
- 依赖 Maildir 布局 `<MAILBOX_ROOT>/<域名>/<用户名>/(cur|new)`(兼容 `Maildir/` 子目录一层);布局变了改 `maildir_path()`
- MIAB 重装/重生成 nginx 配置只覆盖它自己的 `local.conf`,本服务的 `onemail.conf` 是独立文件不受影响
- certbot 证书续期由系统的 `certbot.timer` 负责

## 许可证

MIT,见 [LICENSE](LICENSE)。
