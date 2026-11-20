# MediaClaw 插件仓库

这里是 MediaClaw 的插件源码与签名市场仓库。每个插件独立发版，应用通过 `catalog` Release 中的 `index.json` 获取插件列表。

市场地址：

```text
https://github.com/leaf1022/mediaclaw-plugins/releases/download/catalog/index.json
```

## 当前插件

| 插件 | 分类 | 说明 |
| --- | --- | --- |
| `media-cover-generator` | 媒体库 | 生成四种风格的媒体库横向封面，支持定时刷新 |
| `douban-coming-notice` | 订阅 | 追踪豆瓣即将开播剧集，支持自动订阅和开播提醒 |

## 开发与发版

插件目录采用 schema 2 格式：

```text
plugins/<id>/
├── mediaclaw-plugin.toml
├── store.json
├── CHANGELOG.md
├── icon.svg
├── web/
└── <entry>.py
```

本地校验：

```bash
python3 scripts/generate_index.py --lint
python3 scripts/pack.py plugins/<id> --output dist/<id>-<version>.mcplugin
```

发版 tag 使用 `<id>-v<version>`，标签版本必须与 `mediaclaw-plugin.toml` 和 `CHANGELOG.md` 一致。所有 Release 都使用 `--latest=false`。

