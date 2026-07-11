# SimpMC-Motd

这个 AstrBot 插件会从本机或指定地址获取 Minecraft Java 服务器 MOTD，定时记录在线人数历史，并用 `/motd` 渲染一张状态图片。每个群/会话都可以绑定自己的查询地址，历史记录彼此隔离。

## 安装

把整个 `SimpMC-Motd` 目录放到 AstrBot 的插件目录中，然后在 AstrBot WebUI 启用插件。

默认连接：

- 地址：`127.0.0.1`
- 端口：`25565`
- 采样间隔：`300` 秒
- 图表范围：最近 `24` 小时
- 历史保留：`30` 天
- 渲染缓存：`45` 秒
- 最近采样复用：`30` 秒
- 额外 latency ping：关闭
- 远程背景图缓存：`3600` 秒
- 普通成员可修改本群地址：关闭
- 群白名单：关闭
- 群内 `/setmotd`：开启，默认仅管理员可用

这些都可以在 AstrBot 的插件配置里修改。

## 后台管理

### 白名单模式

开启 `enable_group_whitelist` 后，只有 `group_whitelist` 或 `group_servers_json` 中列出的群可以使用 `/motd`。

`group_whitelist` 支持逗号、空格或换行分隔：

```text
123456789, 987654321
```

也可以写完整形式：

```text
group:123456789
group:987654321
```

### 后台手动配置每个群

在 `group_servers_json` 中填写 JSON。键可以是群号，也可以是 `group:群号`。

```json
{
  "123456789": {
    "address": "mc.example.com:25565",
    "name": "生存服"
  },
  "987654321": "play.example.net:25565"
}
```

如果你想完全禁止群内设置，只允许后台手动配置：

- `enable_setmotd_command` 设为 `false`
- `use_default_server_for_unconfigured_groups` 设为 `false`
- 在 `group_servers_json` 中写入允许查询的群和服务器地址

后台配置优先级最高。某个群已经在 `group_servers_json` 中配置后，群里不能用 `/setmotd` 覆盖，也不能用 `/clearmotd` 清除。

## 命令

- `/motd`：返回 MOTD + 历史在线人数图片。默认优先复用最近 `30` 秒采样，必要时才查询服务器。
- `/setmotd <host[:port]> [名称]`：设置当前群/会话的查询地址，默认仅管理员可用。
- `/clearmotd`：清除当前群/会话的查询地址设置，默认仅管理员可用。

示例：

```text
/setmotd mc.example.com:25565 生存服
/setmotd 127.0.0.1:25565
/motd
/clearmotd
```

## 说明

插件查询的是 Minecraft Java 版 status 协议，不支持 Bedrock 版服务器。

为减少渲染等待，同一群/会话的 `/motd` 默认会缓存 `45` 秒。缓存期内重复查询会直接返回上一张图片，不重新查询服务器和渲染。可以通过 `render_cache_seconds` 调整，设为 `0` 可关闭缓存。

`/motd` 和后台采样默认会复用最近 `30` 秒的状态，避免短时间内重复 ping 同一服务器。可以通过 `sample_reuse_seconds` 调整，设为 `0` 可关闭复用。插件默认不会发送 Minecraft 协议里的额外 latency ping 包；如果确实需要记录延迟，可以把 `send_latency_ping` 设为 `true`。

图片通过 AstrBot 的 HTML 渲染能力生成 PNG，本质上会启动/调用浏览器渲染模板并截图。远程背景图和远程资源会影响首次渲染速度；插件默认会把远程背景缓存 `3600` 秒，后续渲染使用本地 data URI。预取失败时会使用旧缓存；没有旧缓存或没有配置远程背景时，会使用内置背景，避免背景直接消失。可以通过 `background_cache_seconds`、`background_fetch_timeout_seconds` 和 `background_max_bytes` 调整。

状态图会尽量保留 Minecraft MOTD 中的 JSON 颜色和 `§` 颜色码。

状态图中的时间轴和最后采样时间固定使用 `UTC+8 / Asia/Shanghai` 显示，不跟随服务器系统时区。

状态图的 X 轴会按真实采样时间窗口显示时间，不额外标注日期；Y 轴顶部人数按历史在线人数最大值计算，除顶部最大值外，所有可见刻度都会使用 5 的倍数。

历史在线人数不足两条时，图表会用灰色提示暂无历史；当前服务器无法连接时，图表和离线状态会用红色提示。

后台采样数据会保存在 AstrBot 数据目录的 `plugin_data/SimpMC-Motd/history.sqlite3`。插件会按 `group:群号` 隔离群配置和历史；私聊会按私聊会话单独保存。
