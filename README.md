# OneMail 取件服务

把自己邮件服务器上的**真实邮箱**安全地"借"给别人收验证码,而不用交出邮箱密码。

OneMail 运行在 [Mail-in-a-Box](https://mailinabox.email/)(MIAB)服务器上:管理员为任意邮箱生成一条**免登录、只读**的取件链接,对方打开链接就能看到这个邮箱的来信,**验证码会被自动提取并大字展示**,新邮件到达后页面自动刷新。

典型场景:团队或家人共用一个注册邮箱收验证码。

## 目录

- [功能](#功能)
- [快速部署](#快速部署)
- [更新](#更新)
- [配置项](#配置项)
- [安全机制](#安全机制)
- [开发](#开发)
- [MIAB 升级注意事项](#miab-升级注意事项)
- [与同类项目的对比](#与同类项目的对比)
- [许可证](#许可证)

## 功能

### 取件页(`/m/<token>`,免登录)

- **验证码视图**:只列出识别到验证码的邮件,验证码大字显示,一键复制
- **全部邮件视图**:完整收件箱,每页 20 封,最新在前
- **自动收件**:每 2 秒询问一次轻量接口,有新邮件才刷新页面
- **邮件详情**:HTML 正文在沙箱 iframe 里渲染,远端图片默认不加载(点按钮才加载),支持附件下载
- **失效提示**:链接过期、被撤销、不存在时分别显示对应提示页,可配置邮箱地址打码
- 跟随系统的浅色/深色模式

验证码按"关键词 + 邻近的 4~8 位数字"识别(验证码、校验码、动态码、verification code、OTP、passcode、PIN 等),会跳过年份和长订单号。

### 管理端(`/admin/`,用 MIAB 管理员邮箱和密码登录)

- **生成链接**:为任意邮箱生成取件链接,有效期可选 1 天 / 7 天 / 30 天 / 永久
- **管理链接**:复制、延期(URL 不变)、撤销/启用、彻底删除、一键清除已撤销
- **添加邮箱**:调用 MIAB API 建号并预建 Maildir,新邮箱的取件链接立即可用;密码可留空自动生成
- **邮箱备注**与实时搜索
- **访问日志**:每条链接的访问时间、IP、User-Agent

## 快速部署

前提:一台已装好 MIAB 的 Ubuntu 服务器。OneMail 只新增文件,**不改任何 MIAB 管理的配置**。

```bash
# 1. 代码与依赖
mkdir -p /opt/onemail && cp -r app.py templates static /opt/onemail/
apt-get install -y python3-venv
python3 -m venv /opt/onemail/venv
/opt/onemail/venv/bin/pip install -r requirements.txt

# 2. systemd
cp deploy/onemail.service /etc/systemd/system/onemail.service
systemctl daemon-reload && systemctl enable --now onemail

# 3. nginx(MIAB 的 nginx 已在运行,只新增一个独立 conf)
cp deploy/onemail.nginx.conf /etc/nginx/conf.d/onemail.conf
# 把 server_name 改成你的域名,并先把 DNS A 记录指向本机
nginx -t && systemctl reload nginx

# 4. HTTPS
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d <你的域名> --non-interactive --agree-tos -m <管理员邮箱> --redirect

# 5. fail2ban(可选,MIAB 已自带 fail2ban)
cp deploy/fail2ban-filter.conf /etc/fail2ban/filter.d/onemail.conf
cp deploy/fail2ban-jail.conf   /etc/fail2ban/jail.d/onemail.conf
fail2ban-client reload
```

部署完成后访问 `https://<你的域名>/admin/`,用 MIAB 管理员账号登录。

服务器上**不需要 node**:样式是预编译好随仓库提交的,页面也不加载任何外部 CDN。

首次启动会在 `/opt/onemail` 下自动生成 `onemail.db`(sqlite)、`secret_key`、`security.log`,权限均为 600,不进版本库。

## 更新

```bash
cp -r app.py templates static /opt/onemail/
systemctl restart onemail
```

`onemail.db` 等运行时文件不会被覆盖,链接和访问日志都会保留。

## 配置项

都是 `app.py` 顶部的常量,改完重启服务生效。

| 常量 | 默认值 | 说明 |
|---|---|---|
| `MAILBOX_ROOT` | `/home/user-data/mail/mailboxes` | MIAB 的 Maildir 根目录 |
| `MIAB_API` | `https://127.0.0.1/admin/mail/users?format=json` | MIAB 管理 API,用于登录验证和邮箱列表 |
| `MIAB_ADD_API` | `https://127.0.0.1/admin/mail/users/add` | 添加邮箱的 API |
| `MASKED_MAILBOXES` | `{"masked@example.com"}` | 在失效提示页上打码显示的邮箱(如 `ma**@example.com`);链接有效时始终显示完整地址 |
| `PER_PAGE` | `20` | 收件箱每页封数 |
| `TTL_CHOICES` | 1 / 7 / 30 天、永久 | 生成和延期链接时的有效期选项 |
| `USERS_CACHE_SEC` | `300` | 邮箱列表缓存秒数 |
| `LOG_THROTTLE_SEC` | `60` | 同一链接的访问日志节流秒数 |
| `LOGIN_FAIL_*` | 10 分钟内 5 次,锁 15 分钟 | 登录防爆破 |
| `TOKEN404_*` | 每分钟 20 次 | 无效 token 防枚举,超出返回 429 |
| `CODE_SCAN_MAX` | `200000` | 提取验证码时只看正文前多少个字符 |
| `DEV_HOST` / `DEV_PORT` | `127.0.0.1:8200` | 仅 `python app.py` 本地调试用;生产监听地址在 `deploy/onemail.service` 里 |

## 安全机制

**取件链接**

- token 为 192 位随机数;无效 token 按 IP 计数,1 分钟超过 20 次返回 429
- 链接可设有效期、可随时撤销,每次访问都有日志
- 只读:邮件直接从 Maildir 文件读取,不经过 dovecot,不做任何写操作

**邮件渲染**

- HTML 正文放在不允许脚本的 sandbox iframe 里
- 远端图片默认由 CSP 拦截(`img-src` 不含 `https:`),只有点了"加载"才放行
- 附件一律以下载方式返回;邮件文件名做白名单校验,防路径穿越
- HTML 剥标签是线性扫描,验证码提取只看正文前 200KB,恶意邮件无法拖垮服务

**管理端**

- 登录防爆破:按"IP + 账号"计数,10 分钟内失败 5 次锁定 15 分钟
- 会话:cookie 里只有随机 sid(Secure + HttpOnly + SameSite=Lax),12 小时过期
- 所有 POST 校验 CSRF token
- MIAB 管理员密码在会话期内保存在服务端 sqlite 中(调用 MIAB API 需要),库文件权限 600

**服务器层面**

- 响应头:CSP、nosniff、X-Frame-Options DENY、Referrer-Policy no-referrer、HSTS
- fail2ban:独立 jail 读取 `security.log`,10 次命中封 30 分钟;写日志前会清掉换行和控制字符,无法伪造日志行
- systemd 沙箱:NoNewPrivileges、ProtectSystem=full、ProtectHome=read-only 等。服务以 root 运行,因为 MIAB 的 Maildir 目录是 `drwx------ mail:mail`

## 开发

技术栈:Python 3.10+、Flask 3、gunicorn、sqlite3;前端是 Jinja2 模板 + 原生 JS,没有前端框架。

界面有两套样式,互不引用(两个库的 `.card`、`.alert` 等类名会冲突):

| 页面 | 组件库 | 源文件 | 编译产物 |
|---|---|---|---|
| 取件页(收件箱、邮件详情、失效页) | [HeroUI](https://heroui.com/) v3 的纯 CSS 包 `@heroui/styles` | `src/pickup.css` | `static/pickup.css` |
| 管理端、登录页 | [daisyUI](https://daisyui.com/) 5 | `src/app.css` | `static/app.css` |

两者都基于 Tailwind CSS 4。Tailwind 按模板里出现的类名按需生成样式,所以**改完模板要重新编译**,并把编译产物一起提交:

```bash
npm install            # 仅开发依赖
npm run build          # 编译两个样式文件
npm run watch          # 监听管理端样式
npm run watch:pickup   # 监听取件页样式
```

本地运行(需要能访问 MIAB API 和 Maildir,通常直接在服务器上调试):

```bash
pip install -r requirements.txt
python app.py          # 监听 127.0.0.1:8200
```

目录结构:

```
one-mail/
├── app.py                  # 全部后端逻辑(单文件 Flask 应用)
├── templates/
│   ├── pickup_base.html    # 取件页骨架(HeroUI)
│   ├── inbox.html          # 取件页:收件箱
│   ├── message.html        # 取件页:邮件详情
│   ├── invalid.html        # 取件页:链接失效提示
│   ├── base.html           # 管理端骨架(daisyUI)
│   ├── admin_base.html     # 管理端布局(侧栏 + 顶栏)
│   ├── login.html          # 管理员登录
│   ├── admin.html          # 管理端首页
│   ├── links.html          # 链接汇总
│   ├── link_detail.html    # 单条链接的访问日志
│   ├── _icons.html         # 图标(Lucide)
│   └── _toast.html         # 操作结果提示
├── src/                    # 样式源文件
├── static/                 # 编译好的样式和图片,线上直接使用
├── deploy/                 # systemd、nginx、fail2ban 配置
├── package.json            # 仅用于编译样式
└── requirements.txt
```

## MIAB 升级注意事项

- 依赖 MIAB 管理 API 的路径和 JSON 结构(按域名分组,每组有 `users` 数组)。升级后如果管理端登录返回 401 或 500,先检查这个接口
- 依赖 Maildir 布局 `<MAILBOX_ROOT>/<域名>/<用户名>/(cur|new)`,兼容多一层 `Maildir/` 子目录;布局变了改 `maildir_path()`
- MIAB 重装或重新生成 nginx 配置只会覆盖它自己的 `local.conf`,本服务的 `onemail.conf` 不受影响
- 证书续期由系统的 `certbot.timer` 负责

## 与同类项目的对比

| | OneMail | [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) | [AuthInbox](https://github.com/TooonyChen/AuthInbox) |
|---|---|---|---|
| 运行位置 | 自己的邮件服务器(MIAB) | Cloudflare Workers | Cloudflare Workers |
| 用途 | **已有真实邮箱**的只读分享 | 一次性临时邮箱 | 临时接码邮箱 |
| 依赖 | 无第三方云服务,数据自己掌握 | Cloudflare Email Routing | Cloudflare Email Routing |
| 验证码提取 | ✅ | ❌ | ✅ |
| 链接有效期、撤销、访问日志 | ✅ | ❌ | ❌ |
| 账号体系 | 复用 MIAB 管理员账号 | 自建用户系统 | 自建 |

想要零成本的一次性临时邮箱,选 cloudflare_temp_email;想把自己服务器上的真实邮箱借给别人收验证码,选 OneMail。

## 许可证

MIT,见 [LICENSE](LICENSE)。
