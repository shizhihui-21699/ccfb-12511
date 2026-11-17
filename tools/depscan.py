#!/usr/bin/env python3
"""依赖混淆(dependency confusion)扫描器。

流程:
  1. 汇总 company-projects/ 下各项目的依赖清单(go.mod / package.json)
  2. 从本地模拟私有源拉取内部包名清单
  3. 逐个去本地模拟公共源比对:未注册=可被抢注;已存在=已被抢注(比版本)
  4. 解析各项目构建配置(.npmrc / ci.env 里的 GOPROXY、GOPRIVATE 等),
     判断依赖解析会不会落到公网源
  5. 按项目输出风险等级和修复建议(Markdown + JSON)

只依赖标准库。运行前需先启动两个模拟源:
  python3 mock-registries/server.py --port 18081 --npm mock-registries/data/public_npm.json  --go mock-registries/data/public_go.json  --label public
  python3 mock-registries/server.py --port 18082 --npm mock-registries/data/private_npm.json --go mock-registries/data/private_go.json --label private
"""
import fnmatch
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_DIR = os.path.join(ROOT, "company-projects")
REPORT_DIR = os.path.join(ROOT, "report")

PUBLIC_MOCK = "http://127.0.0.1:18081"
PRIVATE_MOCK = "http://127.0.0.1:18082"

# 配置里的真实源地址 -> 本地模拟源(模拟环境映射;真实扫描时直连真实源)
PUBLIC_HOSTS = ("registry.npmjs.org", "registry.npmmirror.com", "goproxy.cn", "proxy.golang.org")
PRIVATE_HOSTS = ("npm.companyx.cn", "goproxy.companyx.cn", "127.0.0.1:18082")

# 内部包判定规则(除"出现在私有源清单"之外)
INTERNAL_SCOPES = ("@hengxin",)
INTERNAL_MODULE_PATTERNS = ("*.companyx.cn/*", "github.com/hengxin-dev/*")

LEVELS = ["低", "中", "高", "严重"]


# ---------- 基础工具 ----------

def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "depscan/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return None, {"error": str(e)}


def mock_base(registry_url):
    """把项目配置里的源地址映射到本地模拟源;无法识别返回 None。"""
    if not registry_url:
        return None
    for h in PRIVATE_HOSTS:
        if h in registry_url:
            return PRIVATE_MOCK
    for h in PUBLIC_HOSTS:
        if h in registry_url:
            return PUBLIC_MOCK
    if "127.0.0.1:18081" in registry_url:
        return PUBLIC_MOCK
    return None


def registry_kind(registry_url):
    for h in PRIVATE_HOSTS:
        if h in registry_url:
            return "private"
    for h in PUBLIC_HOSTS:
        if h in registry_url:
            return "public"
    return "unknown"


def parse_ver(v):
    v = v.strip().lstrip("vV")
    parts = re.split(r"[.\-+]", v)
    out = []
    for p in parts[:4]:
        m = re.match(r"\d+", p)
        out.append(int(m.group()) if m else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def ver_gt(a, b):
    return parse_ver(a) > parse_ver(b)


def npm_satisfies(version, rng):
    """极简 semver range 判断:支持 * / latest / 精确版本 / ^x.y.z / ~x.y.z / >=x.y.z。"""
    rng = (rng or "*").strip()
    if rng in ("*", "", "latest"):
        return True
    v = parse_ver(version)
    m = re.match(r"^\^(\d+)\.(\d+)\.(\d+)$", rng)
    if m:
        base = tuple(int(x) for x in m.groups())
        if base[0] > 0:
            return v >= base and v[0] == base[0]
        return v >= base and v[0] == 0 and v[1] == base[1]
    m = re.match(r"^~(\d+)\.(\d+)\.(\d+)$", rng)
    if m:
        base = tuple(int(x) for x in m.groups())
        return v >= base and v[:2] == base[:2]
    m = re.match(r"^>=(\d+)\.(\d+)\.(\d+)$", rng)
    if m:
        return v >= tuple(int(x) for x in m.groups())
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", rng)
    if m:
        return v == tuple(int(x) for x in m.groups())
    return False  # 复杂 range 不判,宁可漏报满足性(名字被抢注的事实不变)


def best_satisfying(versions, rng):
    ok = [v for v in versions if npm_satisfies(v, rng)]
    return max(ok, key=parse_ver) if ok else None


# ---------- 解析项目文件 ----------

def parse_go_mod(path):
    mods = []
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for m in re.finditer(r"require\s*\(([^)]*)\)", text, re.S):
        for line in m.group(1).splitlines():
            line = line.split("//")[0].strip()
            parts = line.split()
            if len(parts) >= 2:
                mods.append((parts[0], parts[1]))
    for m in re.finditer(r"^\s*require\s+(\S+)\s+(\S+)\s*$", text, re.M):
        mods.append((m.group(1), m.group(2)))
    return mods


def parse_package_json(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    deps = {}
    for key in ("dependencies", "devDependencies"):
        for name, rng in data.get(key, {}).items():
            deps[name] = rng
    return deps


def parse_npmrc(path):
    cfg = {"default": "https://registry.npmjs.org/", "scopes": {}}
    if not os.path.exists(path):
        return cfg
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(("#", ";")) or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            m = re.match(r"^(@[\w.-]+):registry$", k)
            if m:
                cfg["scopes"][m.group(1)] = v
            elif k == "registry":
                cfg["default"] = v
    return cfg


def parse_ci_env(path):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ---------- 远端(模拟源)查询 ----------

def public_npm(name):
    enc = name.replace("/", "%2F")
    return http_get(f"{PUBLIC_MOCK}/npm/{enc}")


def public_go_latest(module):
    return http_get(f"{PUBLIC_MOCK}/go/{module}/@latest")


def public_go_versions(module):
    return http_get(f"{PUBLIC_MOCK}/go/{module}/@v/list")


def private_listing():
    _, npm = http_get(f"{PRIVATE_MOCK}/npm/-/all")
    _, go = http_get(f"{PRIVATE_MOCK}/go/-/modules")
    return set(npm or []), set(go or [])


def private_npm_meta(name):
    enc = name.replace("/", "%2F")
    return http_get(f"{PRIVATE_MOCK}/npm/{enc}")


def private_go_latest(module):
    return http_get(f"{PRIVATE_MOCK}/go/{module}/@latest")


# ---------- 风险判定 ----------

def is_internal_npm(name, private_npm):
    return name in private_npm or any(name.startswith(s + "/") for s in INTERNAL_SCOPES)


def is_internal_go(module, private_go):
    if module in private_go:
        return True
    return any(fnmatch.fnmatchcase(module, p) for p in INTERNAL_MODULE_PATTERNS)


def analyze_npm_project(proj, deps, npmrc, private_npm):
    findings = []
    for name, rng in sorted(deps.items()):
        if not is_internal_npm(name, private_npm):
            continue
        # 解析该包实际会走哪个源
        scope = name.split("/")[0] if name.startswith("@") else None
        reg = npmrc["scopes"].get(scope) if scope else None
        via = reg or npmrc["default"]
        kind = registry_kind(via)

        code, pub = public_npm(name)
        pub_versions = sorted(pub["versions"].keys(), key=parse_ver) if code == 200 else []
        p_code, priv = private_npm_meta(name)
        priv_latest = priv["dist-tags"]["latest"] if p_code == 200 else None

        f = {"package": name, "range": rng, "registry": via, "registry_kind": kind,
             "public_versions": pub_versions, "private_latest": priv_latest}

        if kind == "private":
            if pub_versions:
                f.update(level="中",
                         status="已被抢注(当前配置未受影响)",
                         detail=f"公网已存在同名包(版本 {', '.join(pub_versions)}),"
                                f"但 scope/默认源指向私有源,当前不会拉错。",
                         fix="保持 scope 源映射;建议公司把该名字在公网注册占位,防止他日被利用。")
            else:
                f.update(level="低", status="未被抢注,配置正确",
                         detail="解析走私有源,公网无同名包。",
                         fix="可选:在公网注册同名包占位(防御性抢注)。")
        else:
            if pub_versions:
                hit = best_satisfying(pub_versions, rng)
                if hit:
                    f.update(level="严重",
                             status="已被抢注,构建会拉到公网包",
                             detail=f"解析走公网源({via}),公网同名包版本 {hit} 满足依赖范围 {rng}"
                                    f"(私有最新 {priv_latest}),npm install 会直接装到公网包。",
                             fix="立即在 .npmrc 为内部 scope 配置私有源;已装过的构建机清缓存重装并审计;在公网抢回/占位该名字。")
                else:
                    f.update(level="高",
                             status="已被抢注,版本暂未命中",
                             detail=f"解析走公网源({via}),公网版本 {', '.join(pub_versions)} 暂不满足 {rng},"
                                    f"但名字已被他人控制,对方随时可发布满足范围的版本。",
                             fix="在 .npmrc 为内部 scope 配置私有源;在公网抢回/占位该名字。")
            else:
                f.update(level="高",
                         status="未被抢注,但解析走公网",
                         detail=f"解析走公网源({via}),公网暂无同名包——任何人现在注册该名字,"
                                f"下次构建即被拉取(经典 dependency confusion)。",
                         fix="在 .npmrc 为内部包配置私有源(scope 映射);并立即在公网注册该名字占位。")
        findings.append(f)
    return findings


def go_private_match(module, patterns):
    for p in patterns:
        p = p.strip()
        if not p:
            continue
        # GOPRIVATE 是路径前缀 glob,如 git.companyx.cn 或 github.com/hengxin-dev/*
        if fnmatch.fnmatchcase(module, p) or module.startswith(p.rstrip("*").rstrip("/")):
            return True
    return False


def analyze_go_project(proj, mods, env, private_go):
    findings = []
    goproxy = [s.strip() for s in env.get("GOPROXY", "https://proxy.golang.org,direct").split(",")]
    goprivate = env.get("GOPRIVATE", "")
    gonoproxy = env.get("GONOPROXY", "") or goprivate
    gosumdb = env.get("GOSUMDB", "sum.golang.org")

    for module, req_ver in sorted(mods):
        if not is_internal_go(module, private_go):
            continue
        f = {"package": module, "required": req_ver,
             "goproxy": goproxy, "goprivate": goprivate, "gosumdb": gosumdb}

        code, pub_list = public_go_versions(module)
        pub_versions = sorted(pub_list, key=parse_ver) if code == 200 else []
        _, priv_lat = private_go_latest(module)
        priv_latest = priv_lat.get("Version") if isinstance(priv_lat, dict) else None
        f["public_versions"] = pub_versions
        f["private_latest"] = priv_latest

        if go_private_match(module, gonoproxy.split(",")):
            f.update(level="低", status="GOPRIVATE 覆盖,绕过代理直连",
                     detail="模块命中 GOPRIVATE/GONOPROXY,不走任何代理,公网同名包无影响。",
                     fix="保持;注意确认直连的 VCS 地址确实是内网。")
            findings.append(f)
            continue

        # 按 GOPROXY 顺序模拟 go 命令的解析
        resolution = None  # (proxy, 结果)
        for proxy in goproxy:
            if proxy == "off":
                resolution = ("off", "denied")
                break
            if proxy == "direct":
                if ".companyx.cn" in module:
                    resolution = ("direct", "private-vcs")
                else:
                    resolution = ("direct", "public-vcs")
                break
            kind = registry_kind(proxy)
            base = mock_base(proxy)
            if kind == "private":
                code, lat = http_get(f"{base}/go/{module}/@latest")
                if code == 200:
                    resolution = (proxy, "private-proxy")
                    break
                continue  # 私有源没有则继续下一个
            # 公网代理:查模拟公共源
            code, lat = http_get(f"{PUBLIC_MOCK}/go/{module}/@latest")
            if code == 200:
                resolution = (proxy, "public-proxy-hit", lat.get("Version"))
                break
            continue

        f["resolution"] = resolution
        sumdb_off = gosumdb.lower() == "off"
        sumdb_note = "GOSUMDB=off,校验和数据库防护已关闭。" if sumdb_off else \
            "GOSUMDB 开启,但内部模块未纳入 GOPRIVATE 时校验会失败(表现为构建报错而非拉错包)。"

        if resolution and resolution[1] == "public-proxy-hit":
            pub_ver = resolution[2]
            newer = priv_latest and ver_gt(pub_ver, priv_latest)
            f.update(level="严重",
                     status="已被抢注,构建会拉到公网模块",
                     detail=f"GOPROXY 中 {resolution[0]} 先于私有源命中,公网同名模块最新 {pub_ver}"
                            f"(私有最新 {priv_latest}){'，版本更高,go get -u/@latest 必拉公网包' if newer else ''}。"
                            + sumdb_note,
                     fix="把内网模块路径加入 GOPRIVATE(如 github.com/hengxin-dev/*,*.companyx.cn),"
                         "并把私有代理放到 GOPROXY 首位;开启 GOSUMDB;审计近期构建产物。")
        elif resolution and resolution[1] == "public-vcs":
            f.update(level="高",
                     status="回退到公网 VCS 直连",
                     detail=f"所有代理未命中后 direct 到公网代码托管({module}),"
                            f"该路径不受公司控制,可被注册同名仓库投毒。" + sumdb_note,
                     fix="把该模块路径加入 GOPRIVATE;确认直连目标为内网仓库。")
        elif resolution and resolution[1] == "private-proxy":
            # 公网代理是否排在命中的私有代理之前(若是,名字一旦被抢注即优先命中公网)
            public_ahead = any(
                registry_kind(p) == "public"
                for p in goproxy[:goproxy.index(resolution[0])]
            )
            if pub_versions:
                f.update(level="中",
                         status="已被抢注(当前代理顺序未受影响)",
                         detail=f"私有代理先命中,但公网已存在同名模块 {', '.join(pub_versions)};"
                                f"一旦 GOPROXY 顺序变动或私有源抖动,即可能拉错。",
                         fix="把该模块路径加入 GOPRIVATE 彻底绕开公网;公网路径抢回/占位。")
            elif public_ahead:
                f.update(level="中",
                         status="未被抢注,但公网代理排在私有代理之前",
                         detail=f"公网代理当前无此模块才落到私有代理;任何人抢注 {module} 后,"
                                f"排在前面的公网代理会优先命中,构建即被投毒。",
                         fix="把该模块路径加入 GOPRIVATE;并把私有代理调整到 GOPROXY 首位。")
            else:
                f.update(level="低", status="经私有代理解析,公网无同名",
                         detail="私有代理先命中,公网无同名模块。",
                         fix="可选:加入 GOPRIVATE 双保险。")
        elif resolution and resolution[1] == "private-vcs":
            f.update(level="低", status="直连内网 VCS",
                     detail="代理未命中后 direct 到内网仓库,公网无法投毒;但模块名会泄露给公网代理(404 探测)。",
                     fix="建议加入 GOPRIVATE,避免向公网代理泄露内部模块名。")
        else:
            f.update(level="中", status="解析失败或受限",
                     detail=f"按当前 GOPROXY 无法解析该内部模块(resolution={resolution}),"
                            f"表现为构建失败;若有人调整配置放开公网,名字{'已被抢注' if pub_versions else '可被抢注'}。",
                     fix="修正 GOPROXY/GOPRIVATE;公网占位该名字。")
        findings.append(f)
    return findings


# ---------- 主流程 ----------

def worst_level(findings):
    if not findings:
        return "低"
    return max((f["level"] for f in findings), key=lambda l: LEVELS.index(l))


def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    private_npm, private_go = private_listing()
    print(f"私有源清单: npm {len(private_npm)} 个, go {len(private_go)} 个")

    projects = []
    for name in sorted(os.listdir(PROJECTS_DIR)):
        pdir = os.path.join(PROJECTS_DIR, name)
        if not os.path.isdir(pdir):
            continue
        proj = {"name": name, "findings": []}
        gomod = os.path.join(pdir, "go.mod")
        pkgjson = os.path.join(pdir, "package.json")
        if os.path.exists(gomod):
            proj["type"] = "go"
            env = parse_ci_env(os.path.join(pdir, "build", "ci.env"))
            proj["findings"] = analyze_go_project(name, parse_go_mod(gomod), env, private_go)
        elif os.path.exists(pkgjson):
            proj["type"] = "npm"
            npmrc = parse_npmrc(os.path.join(pdir, ".npmrc"))
            proj["findings"] = analyze_npm_project(name, parse_package_json(pkgjson), npmrc, private_npm)
        else:
            continue
        proj["level"] = worst_level(proj["findings"])
        projects.append(proj)

    # 私有源里有、但任何项目都没引用的名字,也要查一遍公网(可被抢注面)
    used_npm, used_go = set(), set()
    for p in projects:
        for f in p["findings"]:
            (used_go if p["type"] == "go" else used_npm).add(f["package"])
    orphan = []
    for n in sorted(private_npm - used_npm):
        code, pub = public_npm(n)
        orphan.append({"package": n, "ecosystem": "npm",
                       "squatted": code == 200,
                       "public_versions": sorted(pub["versions"].keys(), key=parse_ver) if code == 200 else []})
    for m in sorted(private_go - used_go):
        code, lat = public_go_latest(m)
        orphan.append({"package": m, "ecosystem": "go",
                       "squatted": code == 200,
                       "public_versions": [lat["Version"]] if code == 200 else []})

    result = {"projects": projects, "unreferenced_private_packages": orphan}
    with open(os.path.join(REPORT_DIR, "depscan-report.json"), "w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=2)

    write_markdown(result)
    print(f"报告已生成: {os.path.join(REPORT_DIR, 'dependency-confusion-report.md')}")


def write_markdown(result):
    L = []
    L.append("# 依赖混淆(Dependency Confusion)风险扫描报告\n")
    L.append("公共源与私有源均为本地模拟服务(public: 127.0.0.1:18081, private: 127.0.0.1:18082)。\n")
    L.append("## 风险总览\n")
    L.append("| 项目 | 类型 | 风险等级 | 严重/高/中/低 发现数 |")
    L.append("|---|---|---|---|")
    for p in result["projects"]:
        cnt = {l: 0 for l in LEVELS}
        for f in p["findings"]:
            cnt[f["level"]] += 1
        L.append(f"| {p['name']} | {p['type']} | **{p['level']}** | "
                 f"{cnt['严重']}/{cnt['高']}/{cnt['中']}/{cnt['低']} |")
    L.append("")
    L.append("风险等级定义:**严重**=构建当前就会拉到公网同名包;**高**=名字已被抢注或解析必走公网,随时可被利用;"
             "**中**=公网已存在同名包但当前配置暂未受影响,或存在配置回退隐患;**低**=配置正确,仅剩占位/泄露类建议。\n")

    for p in result["projects"]:
        L.append(f"## 项目:{p['name']}({p['type']})— 风险:{p['level']}\n")
        for f in p["findings"]:
            L.append(f"### [{f['level']}] `{f['package']}` — {f['status']}")
            L.append(f"- 依赖声明:`{f.get('range') or f.get('required')}`")
            if p["type"] == "npm":
                L.append(f"- 实际解析源:`{f['registry']}`({f['registry_kind']})")
            else:
                L.append(f"- GOPROXY:`{', '.join(f['goproxy'])}`;GOPRIVATE:`{f['goprivate'] or '(未设置)'}`;GOSUMDB:`{f['gosumdb']}`")
            pub = f.get("public_versions") or []
            L.append(f"- 公网同名:{('存在,版本 ' + ', '.join(pub)) if pub else '不存在(可被抢注)'}")
            L.append(f"- 私有最新:{f.get('private_latest') or '未知'}")
            L.append(f"- 分析:{f['detail']}")
            L.append(f"- **改法**:{f['fix']}")
            L.append("")

    if result["unreferenced_private_packages"]:
        L.append("## 私有源中未被扫描项目引用、但同样暴露在抢注面下的名字\n")
        L.append("| 包名 | 生态 | 公网状态 |")
        L.append("|---|---|---|")
        for o in result["unreferenced_private_packages"]:
            st = f"已被抢注({', '.join(o['public_versions'])})" if o["squatted"] else "未注册,可被抢注"
            L.append(f"| `{o['package']}` | {o['ecosystem']} | {st} |")
        L.append("")
        L.append("建议:以上名字全部在公网对应生态注册占位(防御性抢注),或确认已下线后从私有源清理。")

    with open(os.path.join(REPORT_DIR, "dependency-confusion-report.md"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
