# nginx 配置

`bwicarus.conf` 是生产环境 `/etc/nginx/sites-enabled/default` 的完整副本。

## 为什么不是 symlink 风格

Debian nginx 默认约定是 `sites-available/` 放配置、`sites-enabled/` 放
symlink 指向 sites-available。但这台机器历史上演变成 sites-available 和
sites-enabled **两份独立文件且 drift**（sites-enabled 是真正生效的版本，
sites-available 是旧版）。

git 这边只跟踪「实际生效」那一份（= sites-enabled/default 内容）。

## 部署

```bash
cp _server_deploy/nginx/bwicarus.conf /etc/nginx/sites-enabled/default
# 同步 sites-available 保持一致（避免下次混乱）
cp _server_deploy/nginx/bwicarus.conf /etc/nginx/sites-available/default
nginx -t            # 语法校验
systemctl reload nginx   # 平滑重载，不断连
```

## 关键 location

| 路径 | upstream | 说明 |
|---|---|---|
| `/login` `/logout` `/register` `/profile` `/admin` | `127.0.0.1:5000` | webapp |
| `/dashboard` `/history` `/private` | `127.0.0.1:5000` | 用户私有数据，模板 fallback |
| `/auth` | `127.0.0.1:5000` | device-link 登录回调 |
| `/control` | `127.0.0.1:5000` | 控制面板 |
| `/api` | `127.0.0.1:5000` | 上传 / nav-links / change-password 等 |
| `/stocks` | `127.0.0.1:5002` | 独立 Flask app（股票工作台） |
| `/static/*` `/qa/*` 等其它静态 | `/var/www/html/` | nginx 直接服务 |

新加 webapp 路由前缀时记得也在这里加 location。

## SSL

走 Let's Encrypt + Certbot 自动签发 + renew。证书在
`/etc/letsencrypt/live/bwicarus.space/`，systemd timer `certbot.timer` 自动续。
