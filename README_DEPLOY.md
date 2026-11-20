# 部署到阿里云服务器（远程手机访问）

本指南面向阿里云 ECS 2G2核 Linux3，把记账系统部署成常驻服务，保证手机浏览器远程访问。

---

## 一、服务器准备

### 1.1 阿里云控制台

1. 安全组放行端口：**80 / 443 / 22**（22 SSH，80/443 HTTP/HTTPS）。
2. 记录实例的**公网 IP**（如 `47.xxx.xxx.xxx`）。

### 1.2 安装 Python 与依赖

```bash
# CentOS/Rocky/AlmaLinux（阿里云 Linux3 系）
sudo yum install -y python3 python3-pip

# 升级 pip
python3 -m pip install --upgrade pip

# 安装项目依赖
pip install fastapi "uvicorn[standard]" sqlalchemy pandas openpyxl jinja2 python-multipart
```

### 1.3 上传代码到服务器

本地打包上传（在本地 Windows PowerShell 执行）：
```powershell
# 在项目根目录 F:\myself_project\bill
tar czf bill.tar.gz --exclude='data/*.db' --exclude='__pycache__' --exclude='samples' .
# 上传（替换 user@公网IP）
scp bill.tar.gz user@47.xxx.xxx.xxx:/home/user/
```

服务器上解压：
```bash
cd /home/user
tar xzf bill.tar.gz
# 建议放到独立目录
mv bill bill-app 2>/dev/null; cd bill-app
```

---

## 二、数据库配置

本项目默认用 SQLite（单文件，个人记账足够）。云上同样可用 SQLite，无需额外安装数据库服务。

编辑 `app/config.py` 或创建 `.env`，确认数据目录可写：

```bash
# 确认 data 目录存在且可写
mkdir -p data && chmod 755 data
```

> 如果将来数据量大或多设备并发，可切换 PostgreSQL：在服务器装 `postgresql`，把 `app/database.py` 的连接串改为
> `postgresql+psycopg2://user:pass@localhost/bill`，并 `pip install psycopg2-binary`。

---

## 三、用 systemd 常驻 uvicorn

让服务开机自启、崩溃自动重启。

```bash
# 1. 创建服务单元
sudo tee /etc/systemd/system/bill.service > /dev/null <<'EOF'
[Unit]
Description=个人记账系统 (uvicorn)
After=network.target

[Service]
Type=simple
User=user
WorkingDirectory=/home/user/bill-app
ExecStart=/usr/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# 2. 启动并设为开机自启
sudo systemctl daemon-reload
sudo systemctl enable --now bill.service

# 3. 查看状态
sudo systemctl status bill.service
```

确认 uvicorn 只监听内网 8000，**不直接对外**——对外流量走 Nginx。

---

## 四、Nginx 反向代理 + HTTPS

手机浏览器访问 `https://你的域名`，由 Nginx 转发到 uvicorn，并启用 HTTPS 加密。

### 4.1 安装 Nginx 与 certbot

```bash
sudo yum install -y nginx certbot python3-certbot-nginx
sudo systemctl enable --now nginx
```

### 4.2 配置反向代理

```bash
sudo tee /etc/nginx/conf.d/bill.conf > /dev/null <<'EOF'
server {
    listen 80;
    server_name bill.example.com;   # 换成你的域名

    # 上传账单文件较大，放宽限制
    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
EOF

sudo nginx -t && sudo systemctl reload nginx
```

### 4.3 申请 HTTPS 证书（Let's Encrypt，免费）

```bash
sudo certbot --nginx -d bill.example.com
# 按提示选邮箱、同意协议。certbot 会自动改写 Nginx 启用 443。
```

### 4.4 自动续期

Let's Encrypt 证书 90 天到期，certbot 自带定时任务：
```bash
sudo certbot renew --dry-run   # 测试续期
```

---

## 五、远程手机访问

部署完成后，手机访问方式（任选其一）：

### 方案 A：有域名（推荐）
- 在域名 DNS 添加 A 记录指向服务器公网 IP。
- 手机浏览器直接访问 `https://bill.example.com`。
- 全站 HTTPS，安全。

### 方案 B：无域名，用公网 IP + 自签证书（不推荐长期使用）
- 访问 `http://47.xxx.xxx.xxx`（仅 HTTP，账单数据明文传输，仅应急）。
- 浏览器会提示不安全。

### 方案 C：无域名 + frp 内网穿透（家庭网络场景）
如果服务器不在公网或想用域名，可自建 frp：
- 服务器端跑 frps，本地/路由器跑 frpc 映射 80/443。
- 仍建议配域名 + certbot。

> 安全提醒：若用 HTTP 或自签证书，手机浏览器访问会告警，且账单数据明文。**优先方案 A**。

---

## 六、数据备份

SQLite 数据在 `data/bill.db`（或你配置的路径）。定期备份：

```bash
# 加入 crontab，每日 3 点备份
sudo tee /etc/cron.d/bill-backup > /dev/null <<'EOF'
0 3 * * * root cp /home/user/bill-app/data/bill.db /home/user/bill-app/data/bill-$(date +\%Y\%m\%d).db
EOF
```

---

## 七、验证部署

1. 服务器本地：`curl http://127.0.0.1:8000/api/reports/monthly?month=2026-11`
2. 公网：手机浏览器访问 `https://你的域名/`
3. 上传一份微信账单 Excel 测试导入与去重。

---

## 附：本地 Windows 启动（对照）

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# 本机访问 http://127.0.0.1:8000
# 同局域网手机访问 http://192.168.x.x:8000
```
