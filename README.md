# Minecraft MOTD Status for AstrBot

这个 AstrBot 插件会从本机或指定地址获取 Minecraft Java 服务器 MOTD，定时记录在线人数历史，并用 `/motd` 渲染一张状态图片。每个群/会话都可以绑定自己的查询地址，历史记录彼此隔离。

## 安装

把整个 `astrbot_plugin_mc_motd` 目录放到 AstrBot 的插件目录中，然后在 AstrBot WebUI 启用插件。

默认连接：

- 地址：`127.0.0.1`
- 端口：`25565`
- 采样间隔：`300` 秒
- 图表范围：最近 `24` 小时
- 历史保留：`30` 天
- 普通成员可修改本群地址：开启

这些都可以在 AstrBot 的插件配置里修改。

## 命令

- `/motd`：立即查询一次服务器状态，并返回 MOTD + 历史在线人数图片。
- `/setmotd <host[:port]> [名称]`：设置当前群/会话的查询地址。
- `/clearmotd`：清除当前群/会话的查询地址设置，下次查询会使用插件默认地址。

示例：

```text
/setmotd mc.example.com:25565 生存服
/setmotd 127.0.0.1:25565
/motd
/clearmotd
```

## 说明

插件查询的是 Minecraft Java 版 status 协议，不支持 Bedrock 版服务器。

后台采样数据会保存在 AstrBot 数据目录的 `plugin_data/astrbot_plugin_mc_motd/history.sqlite3`。插件会按 `平台ID + 群ID` 隔离配置和历史；私聊会按私聊会话单独保存。
