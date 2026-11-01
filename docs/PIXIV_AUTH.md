# Pixiv 认证：仅使用 Refresh Token / Cookie

账号密码登录、GPPT、浏览器模拟登录和 TOTP 已移除。

- `PIXIV_REFRESH_TOKEN`：手工配置的 Pixiv App API Refresh Token（下载小说和插画需要）。
- `PIXIV_COOKIE`：可选浏览器 Cookie，供 Web AJAX 查询漫画系列和动图信息。
- `API_KEY`：后端 API 的独立访问密钥，仍需配置且长度至少 32 字符；不会因为移除 Pixiv 密码登录而取消。

`GET /api/admin/pixiv/auth/status` 查询两种凭据的有效性；
`POST /api/admin/pixiv/auth/refresh` 仅重新验证已配置的 Token/Cookie，**不会自动生成新凭据**。

Token/Cookie 必须由使用者在服务外更新，修改 `.env` 后重启容器。所有接口仍使用 `X-API-Key` 鉴权。
