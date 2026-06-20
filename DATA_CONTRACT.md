# WeReadNext 数据契约

这个工程只负责把微信读书数据稳定同步到 Notion 数据库。页面布局、首页、菜单、图表和视图由 Notion 或 Notion AI 负责。

## 当前数据库

`init` 只维护下面这些 `微信读书数据库` 数据库。再次运行 `init` 会先查找已有同名且兼容的数据库，只有缺失时才创建。旧的 `WeReadNext v3`/`WeReadenext v3` 名称只作为迁移兼容，运行 `init` 后会尽量重命名成新名称。

| 数据库 | 主要用途 | 关键字段 |
| --- | --- | --- |
| 书架 | 全量书架、在读/已读/想读、进度、封面、统计计数 | 书名、BookId、ISBN、链接、作者、分类、Sort、阅读状态、阅读时长、阅读进度、阅读天数、开始阅读时间、最后阅读时间、书架分类、来源、我的评分、置顶、私密、划线数、笔记数、书签数 |
| 作者 | 作者维表 | 名称 |
| 分类 | 分类维表 | 名称 |
| 章节 | 每本书目录 | Name、bookId、chapterUid、chapterIdx、level、updateTime、sortKey、书籍 |
| 划线 | 个人划线内容 | Name、bookId、bookmarkId、range、chapterUid、Date、style、colorStyle、sortKey、书籍 |
| 笔记 | 个人想法、点评、书评 | Name、bookId、reviewId、range、chapterUid、Date、star、abstract、sortKey、书籍 |
| 日 | 每日阅读时长 | 标题、日期、时间戳、时长、年、月、周；时长按分钟写入 |
| 周 | 周期聚合维表 | 标题、日期、时长 |
| 月 | 周期聚合维表 | 标题、日期、时长 |
| 年 | 周期聚合维表 | 标题、日期、时长 |
| 阅读记录 | 预留阅读记录明细 | 标题、日期、时间戳、时长、书籍 |

## Skill 支持但当前未完整入库的数据

这些数据在微信读书 Gateway 或 skill 文档里能拿到，但不一定都适合默认开启。

### 建议默认增强到现有库

这些字段属于用户自己的书架、书籍、章节、进度和笔记数据，稳定且适合作为数据库字段。

| 数据 | 来源接口 | 建议入库位置 |
| --- | --- | --- |
| 译者、出版社、出版时间、总字数、评分人数、评分分布 | `/book/info` | 书架 |
| 当前章节、章节内偏移、是否已开始阅读、最后阅读时间、读完时间、服务端时间戳 | `/book/getprogress` | 书架 |
| 每本书累计阅读时长、阅读天数 | `/book/getprogress` | 书架 |
| 我的评分 | `/book/info` 的 `newRatingDetail.myRating` | 书架；按 `1 星` 到 `5 星` 写入 |
| 章节字数、章节价格、是否已购买、是否公众号章节 | `/book/chapterinfo` | 章节 |
| 章节内锚点/子标题 | `/book/chapterinfo` | 如果数量少可放章节；数量多建议新建“章节锚点”库 |
| 有声书专辑 ID、集数、完结状态、付费类型、是否已购买、最近收听时间 | `/shelf/sync` | 书架，来源为 album |
| 文章收藏入口是否存在 | `/shelf/sync` | 书架，来源为 mp；只能表示入口，不含文章明细 |
| 公开/私密阅读口径 | `/shelf/sync` | 书架或统计快照；mp 非空时计入私密 |
| 笔记本总览的总笔记数、划线数、想法数、书签数、最近笔记排序值 | `/user/notebooks` | 书架；也可新增“笔记本概览快照” |
| 划线深度链接、章节深度链接、书籍深度链接 | skill URL Schema | 书架、章节、划线、笔记 |

### 建议做成可选同步库

这些数据有价值，但体量更大、更新口径更复杂，或包含公开聚合数据，不建议默认每次同步。

| 数据库建议 | 来源接口 | 字段/内容 |
| --- | --- | --- |
| 阅读统计快照 | `/readdata/detail` | mode、baseTime、readDays、totalReadTime、dayAverageReadTime、compare、readRate、wrReadTime、wrListenTime、registTime |
| 阅读分桶 | `/readdata/detail` | readTimes、dailyReadTimes；可作为日/周/月/年热力图数据源 |
| 阅读排行 | `/readdata/detail` | readLongest 的书籍/有声书、readTime、recordReadingTime、tags |
| 阅读统计项 | `/readdata/detail` | readStat 里的“读过、读完、阅读、笔记”等统计文案和跳转 scheme |
| 偏好分类 | `/readdata/detail` | categoryId、categoryTitle、parentCategory、val、readingTime、readingCount、categoryType |
| 偏好时段 | `/readdata/detail` | 24 小时分布 preferTime、preferTimeWord；注意顺序从 6 点开始 |
| 偏好作者 | `/readdata/detail` | authorId、name、count、readTime、user |
| 偏好出版社 | `/readdata/detail` | name、count |
| 偏好版权方 | `/readdata/detail` | copyrightInfo、count |
| 年度报告入口 | `/readdata/detail` | yearReport、times；活动字段会变，建议只保存入口和月份时长 |
| 勋章 | `/readdata/detail` | medals；有展示阈值，字段可能不稳定 |
| 热门划线 | `/book/bestbookmarks` | markText、totalCount、range、chapterUid、代表用户 vid、繁简 range |
| 章节划线热度 | `/book/underlines` | range、count、score、type、synckey；不含文本 |
| 划线下公开想法 | `/book/readreviews` | range、totalCount、pageReviews、作者、内容、创建时间 |
| 单条想法详情 | `/review/single` | htmlContent、评论/点赞分页 synckey；适合按需补全 |
| 公开书评摘要 | `/review/list` | reviewsCnt、recentTotalCnt、好友点评数、资深会员推荐比例 |
| 公开书评明细 | `/review/list` | reviewId、content、htmlContent、star、isFinish、author、createTime |

### 不建议默认入库

这些接口更像临时查询/发现能力，默认写入 Notion 容易污染个人知识库。

| 数据 | 来源接口 | 原因 |
| --- | --- | --- |
| 搜索结果 | `/store/search` | 与个人资产无关，结果会随时间变化 |
| 个性化推荐 | `/book/recommend` | 推荐流动态变化，容易膨胀 |
| 相似书推荐 | `/book/similar` | 适合临时查看，不适合作为默认同步数据 |
| 公开评论全量抓取 | `/review/list` | 不是个人数据，分页量可能大 |
| 划线下评论全量抓取 | `/book/readreviews` | 不是个人数据，数量可能大 |

## 防重复库规则

- `init` 只创建缺失数据库，不会为了修复 schema 重新创建已有库。
- 查找顺序是：`.env` 中固定的数据源 ID、`NOTION_PAGE` 下递归查找、全工作区精确标题搜索。
- 只有标题匹配且核心字段兼容的数据库才会被复用。
- `doctor` 会报告重复的 `微信读书数据库` 数据库，例如 `微信读书数据库 书架 x2`。
- 如果 Notion AI 移动数据库，建议保留数据库标题和字段不变；同步器仍能通过精确标题搜索找回来。
- 不建议手动复制 `微信读书数据库` 数据库。需要备份时先复制页面布局，不复制系统数据表。
