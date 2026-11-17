# 依赖混淆(Dependency Confusion)风险扫描报告

公共源与私有源均为本地模拟服务(public: 127.0.0.1:18081, private: 127.0.0.1:18082)。

## 风险总览

| 项目 | 类型 | 风险等级 | 严重/高/中/低 发现数 |
|---|---|---|---|
| admin-console | npm | **严重** | 2/1/0/0 |
| customer-h5 | npm | **严重** | 1/0/1/0 |
| order-service | go | **严重** | 1/0/1/1 |
| payment-gateway | go | **严重** | 1/0/0/1 |

风险等级定义:**严重**=构建当前就会拉到公网同名包;**高**=名字已被抢注或解析必走公网,随时可被利用;**中**=公网已存在同名包但当前配置暂未受影响,或存在配置回退隐患;**低**=配置正确,仅剩占位/泄露类建议。

## 项目:admin-console(npm)— 风险:严重

### [高] `@hengxin/ui-kit` — 未被抢注,但解析走公网
- 依赖声明:`^2.3.0`
- 实际解析源:`https://registry.npmjs.org/`(public)
- 公网同名:不存在(可被抢注)
- 私有最新:2.3.1
- 分析:解析走公网源(https://registry.npmjs.org/),公网暂无同名包——任何人现在注册该名字,下次构建即被拉取(经典 dependency confusion)。
- **改法**:在 .npmrc 为内部包配置私有源(scope 映射);并立即在公网注册该名字占位。

### [严重] `@hengxin/utils` — 已被抢注,构建会拉到公网包
- 依赖声明:`^1.4.0`
- 实际解析源:`https://registry.npmjs.org/`(public)
- 公网同名:存在,版本 1.0.0, 1.9.0, 3.9.9
- 私有最新:1.4.2
- 分析:解析走公网源(https://registry.npmjs.org/),公网同名包版本 1.9.0 满足依赖范围 ^1.4.0(私有最新 1.4.2),npm install 会直接装到公网包。
- **改法**:立即在 .npmrc 为内部 scope 配置私有源;已装过的构建机清缓存重装并审计;在公网抢回/占位该名字。

### [严重] `hx-tracker` — 已被抢注,构建会拉到公网包
- 依赖声明:`^1.2.0`
- 实际解析源:`https://registry.npmjs.org/`(public)
- 公网同名:存在,版本 1.5.0, 2.0.1
- 私有最新:1.2.0
- 分析:解析走公网源(https://registry.npmjs.org/),公网同名包版本 1.5.0 满足依赖范围 ^1.2.0(私有最新 1.2.0),npm install 会直接装到公网包。
- **改法**:立即在 .npmrc 为内部 scope 配置私有源;已装过的构建机清缓存重装并审计;在公网抢回/占位该名字。

## 项目:customer-h5(npm)— 风险:严重

### [中] `@hengxin/utils` — 已被抢注(当前配置未受影响)
- 依赖声明:`^1.4.0`
- 实际解析源:`https://npm.companyx.cn`(private)
- 公网同名:存在,版本 1.0.0, 1.9.0, 3.9.9
- 私有最新:1.4.2
- 分析:公网已存在同名包(版本 1.0.0, 1.9.0, 3.9.9),但 scope/默认源指向私有源,当前不会拉错。
- **改法**:保持 scope 源映射;建议公司把该名字在公网注册占位,防止他日被利用。

### [严重] `hx-tracker` — 已被抢注,构建会拉到公网包
- 依赖声明:`^1.2.0`
- 实际解析源:`https://registry.npmmirror.com`(public)
- 公网同名:存在,版本 1.5.0, 2.0.1
- 私有最新:1.2.0
- 分析:解析走公网源(https://registry.npmmirror.com),公网同名包版本 1.5.0 满足依赖范围 ^1.2.0(私有最新 1.2.0),npm install 会直接装到公网包。
- **改法**:立即在 .npmrc 为内部 scope 配置私有源;已装过的构建机清缓存重装并审计;在公网抢回/占位该名字。

## 项目:order-service(go)— 风险:严重

### [低] `git.companyx.cn/common/golog` — GOPRIVATE 覆盖,绕过代理直连
- 依赖声明:`v0.8.0`
- GOPROXY:`https://goproxy.cn, https://goproxy.companyx.cn, direct`;GOPRIVATE:`git.companyx.cn`;GOSUMDB:`off`
- 公网同名:不存在(可被抢注)
- 私有最新:v0.8.0
- 分析:模块命中 GOPRIVATE/GONOPROXY,不走任何代理,公网同名包无影响。
- **改法**:保持;注意确认直连的 VCS 地址确实是内网。

### [严重] `github.com/hengxin-dev/authlib` — 已被抢注,构建会拉到公网模块
- 依赖声明:`v1.1.0`
- GOPROXY:`https://goproxy.cn, https://goproxy.companyx.cn, direct`;GOPRIVATE:`git.companyx.cn`;GOSUMDB:`off`
- 公网同名:存在,版本 v1.0.0, v1.9.0
- 私有最新:v1.2.0
- 分析:GOPROXY 中 https://goproxy.cn 先于私有源命中,公网同名模块最新 v1.9.0(私有最新 v1.2.0)，版本更高,go get -u/@latest 必拉公网包。GOSUMDB=off,校验和数据库防护已关闭。
- **改法**:把内网模块路径加入 GOPRIVATE(如 github.com/hengxin-dev/*,*.companyx.cn),并把私有代理放到 GOPROXY 首位;开启 GOSUMDB;审计近期构建产物。

### [中] `github.com/hengxin-dev/httpx` — 未被抢注,但公网代理排在私有代理之前
- 依赖声明:`v0.5.0`
- GOPROXY:`https://goproxy.cn, https://goproxy.companyx.cn, direct`;GOPRIVATE:`git.companyx.cn`;GOSUMDB:`off`
- 公网同名:不存在(可被抢注)
- 私有最新:v0.5.0
- 分析:公网代理当前无此模块才落到私有代理;任何人抢注 github.com/hengxin-dev/httpx 后,排在前面的公网代理会优先命中,构建即被投毒。
- **改法**:把该模块路径加入 GOPRIVATE;并把私有代理调整到 GOPROXY 首位。

## 项目:payment-gateway(go)— 风险:严重

### [低] `git.companyx.cn/common/golog` — 直连内网 VCS
- 依赖声明:`v0.8.0`
- GOPROXY:`https://goproxy.cn, direct`;GOPRIVATE:`(未设置)`;GOSUMDB:`sum.golang.org`
- 公网同名:不存在(可被抢注)
- 私有最新:v0.8.0
- 分析:代理未命中后 direct 到内网仓库,公网无法投毒;但模块名会泄露给公网代理(404 探测)。
- **改法**:建议加入 GOPRIVATE,避免向公网代理泄露内部模块名。

### [严重] `github.com/hengxin-dev/authlib` — 已被抢注,构建会拉到公网模块
- 依赖声明:`v1.2.0`
- GOPROXY:`https://goproxy.cn, direct`;GOPRIVATE:`(未设置)`;GOSUMDB:`sum.golang.org`
- 公网同名:存在,版本 v1.0.0, v1.9.0
- 私有最新:v1.2.0
- 分析:GOPROXY 中 https://goproxy.cn 先于私有源命中,公网同名模块最新 v1.9.0(私有最新 v1.2.0)，版本更高,go get -u/@latest 必拉公网包。GOSUMDB 开启,但内部模块未纳入 GOPRIVATE 时校验会失败(表现为构建报错而非拉错包)。
- **改法**:把内网模块路径加入 GOPRIVATE(如 github.com/hengxin-dev/*,*.companyx.cn),并把私有代理放到 GOPROXY 首位;开启 GOSUMDB;审计近期构建产物。

## 私有源中未被扫描项目引用、但同样暴露在抢注面下的名字

| 包名 | 生态 | 公网状态 |
|---|---|---|
| `@hengxin/charts` | npm | 未注册,可被抢注 |

建议:以上名字全部在公网对应生态注册占位(防御性抢注),或确认已下线后从私有源清理。
