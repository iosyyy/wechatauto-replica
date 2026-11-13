# 官方 Linux 微信远程登录

这只是官方客户端的私人图形会话。它不自动取钥、不读取聊天、不发消息，
也不代表 Linux 到 LangBot 的桥接已经接通。当前目标为 x86_64。

从 [腾讯官方页面](https://linux.weixin.qq.com/) 下载 x86_64 DEB，保存为
`client.deb`。核对安装包元数据和 SHA256 后构建：

```bash
docker build -t local/wechatauto-linux-desktop:0.1.0 .
```

下载缓慢时可显式传入 `--build-arg DEBIAN_MIRROR=https://mirrors.tencent.com`，
APT 仍验证 Debian 签名。依赖列表包含 `libpulse0`：客户端会动态加载它，
仅检查 DEB 的 `Depends` 或 ELF 的 `ldd` 输出不足以确认完整的运行依赖。

初始化 `.runtime/home` 为 UID/GID 10001 所有，目录权限 0700。
在 `.runtime/desktop-password.txt` 写入随机生成的八位字母数字口令，无换行亦可；
文件由 UID 10001 所有、权限 0600。另生成一次 32 位十六进制
`.runtime/machine-id` 并持久保留，权限 0600、UID 10001 所有。
口令、机器标识、登录态和聊天数据库不要提交 Git、输出到聊天或放入镜像。

```bash
docker compose up -d
ssh -N -L 16081:127.0.0.1:16081 your-server
```

本机打开 `http://127.0.0.1:16081/vnc.html?autoconnect=1&resize=scale`，
输入私人桌面口令后，在官方微信窗口用手机扫码确认。口令不要放进 URL。
手机确认后若桌面仍显示“登录”，还需点击该按钮，等待进入会话界面。
不能仅凭二维码消失或账号文件出现就判断桌面端已完成登录。
远程访问经 SSH 加密，原始 VNC 仅监听容器回环，网页端口只绑定宿主回环。
不要把 6080 或 5900 直接公开。

客户端以普通用户运行，不挂载 Docker socket、不使用 privileged，也不接入
LangBot 的服务网络。全部账号文件在持久化的 `.runtime/home`；关闭本机网页或
SSH 通道不会停止服务器客户端。日志有大小上限并保存在私人目录。

Docker 健康状态只说明桌面进程和网页入口可用，不代表微信账号已登录。
实际账号登录、数据库适配、合法密钥来源、文本发送和 LangBot 桥接分别验收。
