# AstrBot 萌娘百科插件

一个可直接放入 AstrBot 插件目录的萌娘百科查询插件，同时附带 Codex MCP 适配层。

## 功能

- `/萌娘搜索 关键词`：按关键词筛选最相关的主体词条，并生成一张独立 PNG 信息卡。
- `/萌娘词条 词条名`：读取指定词条并生成一张独立 PNG 信息卡。
- 信息卡会优先读取词条的主图（`og:image`），适用于人物头像、动漫/漫画封面、小说封面等；没有合适图片时自动使用纯文字卡片。
- Codex MCP 工具 `moegirl_search` / `moegirl_page`：与 AstrBot 使用同一套网页解析逻辑。

## 安装和运行

将整个目录复制到 AstrBot 的插件目录并重载插件即可。插件会通过 `certifi` 使用受信任的 CA 证书包，并用 Pillow 生成 PNG 信息卡；请让 AstrBot 安装 `requirements.txt` 中的依赖。

搜索会自动排除“分类、列表、章节、目录、模板、特殊页面”等导航型结果，优先选择标题精确匹配的独立人物或作品主体。

图片来源仅限萌娘百科页面公开提供的图片；插件会跳过 SVG、站点 Logo、消歧义占位图和默认占位图。

插件入口使用 AstrBot 新版兼容导入：`astrbot.api.event` 与 `astrbot.api.star`。如果你的 AstrBot 版本非常旧，建议先升级 AstrBot；旧版 API 使用 `astrbot.api.all` 的写法与当前版本不兼容。

Codex 侧使用 MCP stdio 服务端；若运行环境中的 Python 命令名不同，可把 `.mcp.json` 中的 `command` 改成对应的 Python 可执行文件路径。

萌娘百科当前对匿名 `api.php` 的 `query`、`parse` action 返回 `Unauthorized API call`，所以插件使用公开的 `Special:搜索` 和 `/wiki/词条名` 页面读取数据；不需要 API 密钥。请求设置了 20 秒网络超时，并限制搜索最多返回 10 条结果。网络不可用或词条不存在时，工具会返回可读的错误信息，不会让 MCP 进程退出。

## API 来源

- 搜索页：`https://zh.moegirl.org.cn/index.php?title=Special:搜索`
- 词条页：`https://zh.moegirl.org.cn/wiki/词条名`
