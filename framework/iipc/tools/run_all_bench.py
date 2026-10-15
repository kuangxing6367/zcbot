# -*- coding: utf-8 -*-
"""IIPC 一键基准总跑：构建 → 单元 → 三条端到端基准 →（可选）调试包冒烟。

用法：
    python iipc/tools/run_all_bench.py                # 全部步骤
    python iipc/tools/run_all_bench.py --skip-build   # 跳过编译（只跑基准）
    python iipc/tools/run_all_bench.py --pack         # 追加调试包冒烟（_dist）
    python iipc/tools/run_all_bench.py --only e2e,qps # 只跑指定步骤

退出码：任一步骤失败 = 1；全绿 = 0。
每步的输出实时打到 stdout，末尾汇总「步骤 / 结果 / 耗时 / 摘要」。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
IIPC = os.path.dirname(HERE)                       # .../python_side/iipc
PY_SIDE = os.path.dirname(IIPC)                    # .../python_side
ROOT = os.path.dirname(PY_SIDE)                    # 仓库根
KERNEL_DIR = os.path.join(IIPC, "go_kernel")
# 协议端源码自带在 iipc/rust_adapter；旧布局（python_side/core_plugins/…）回退兼容
RUST_DIR = next(
    (c for c in (
        os.path.join(IIPC, "rust_adapter"),
        os.path.join(PY_SIDE, "core_plugins", "IIPC_onebot_adapter"),
    ) if os.path.isdir(c)),
    os.path.join(IIPC, "rust_adapter"))
DIST = os.path.join(ROOT, "_dist", "zcbot_iipc")
PY = sys.executable


def _find_go():
    for cand in (os.environ.get("ZCBOT_GO_BIN"), r"E:/make_path/go/bin/go.exe",
                 shutil.which("go"), shutil.which("go.exe")):
        if cand and os.path.exists(cand):
            return cand
    return "go"


GO_BIN = _find_go()
CARGO = os.environ.get("ZCBOT_CARGO") or shutil.which("cargo") or r"/e/make_path/rust/.cargo/bin/cargo"

# 探针端口：各脚本自挑随机端口，互不冲突；此处仅用于失败时提示
STEPS = ("build-go", "test-go", "build-rust", "e2e", "official", "surface",
         "adapter", "latency", "stress", "pack")


def run(cmd, cwd, timeout, env=None, tag=""):
    """执行并实时回显；返回 (rc, 全部输出)。"""
    t0 = time.time()
    print("\n----- [%s] %s (cwd=%s) -----" % (tag, " ".join(cmd), cwd))
    sys.stdout.flush()
    try:
        p = subprocess.run(cmd, cwd=cwd, timeout=timeout, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace")
        out, rc = p.stdout or "", p.returncode
    except subprocess.TimeoutExpired as e:
        out, rc = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), 124
    except FileNotFoundError as e:
        out, rc = "命令不存在: %s" % e, 127
    for line in (out or "").splitlines():
        print(line)
    sys.stdout.flush()
    return rc, out, time.time() - t0


def summarize(tag, out):
    """从输出里抓关键指标做摘要（抓不到就给首行）。"""
    m = re.search(r"====\s*(\d+)\s*项，通过\s*(\d+)，失败\s*(\d+)", out)
    if m:
        return "%s 项（通过 %s / 失败 %s）" % (m.group(1), m.group(2), m.group(3))
    m = re.search(r"总计\s*(\d+)\s*PASS\s*/\s*(\d+)\s*FAIL", out)
    if m:
        return "总计 %s PASS / %s FAIL" % (m.group(1), m.group(2))
    m = re.search(r"====\s*(\d+)\s*PASS\s*/\s*(\d+)\s*FAIL", out)
    if m:
        extra = ""
        t = re.search(r"饱和吞吐:\s*([\d.]+)", out)
        p = re.search(r"P95=([\d.]+)s", out)
        if t:
            extra += " 吞吐%s条/s" % t.group(1)
        if p:
            extra += " P95=%ss" % p.group(1)
        return "%s PASS / %s FAIL%s" % (m.group(1), m.group(2), extra or "")
    m = re.search(r"首条 send_msg 延迟:\s*(\S+)", out)
    if m:
        return "首条回传延迟 %s" % m.group(1)
    fail = [l for l in out.splitlines() if "FAIL" in l]
    if fail:
        return "FAIL×%d: %s" % (len(fail), fail[0][:60])
    for l in out.splitlines():
        if l.strip():
            return l.strip()[:70]
    return ""


def parse_metrics(out):
    """从 stress_check 输出里抓「三口径」指标，抓不到返回 None。"""
    m = re.search(r"轻载:\s*(\d+)/(\d+)\s*收到；P50=([\d.]+)s\s*P95=([\d.]+)s\s*P99=([\d.]+)s",
                  out)
    if not m:
        return None
    lat = dict(got=int(m.group(1)), n=int(m.group(2)), p50=float(m.group(3)),
               p95=float(m.group(4)), p99=float(m.group(5)))
    t = re.search(r"饱和吞吐:\s*([\d.]+)\s*条/秒（(\d+)/(\d+)\s*收到，端到端\s*span=([\d.]+)s）",
                  out)
    thr = None
    if t:
        thr = dict(tps=float(t.group(1)), got=int(t.group(2)), n=int(t.group(3)),
                   span=float(t.group(4)))
    return {"lat": lat, "thr": thr}


METRIC_NOTE = (
    "口径（三条互不替代，别混着比）\n"
    "  ① 延迟：轻载限速发送、队列不积压时的**单条真实体验**；计时起点取事件**入队时刻**\n"
    "     （不是取出时刻，否则把排队时间算没了），终点为回显到达。\n"
    "  ② 吞吐：饱和灌入的**稳态产能**，按端到端 span（首条发出→末条回显）算，\n"
    "     不是「首尾回复时间差」那种产出速率口径（后者会虚高一个量级）。\n"
    "  ③ 可靠性：轻载/突发两段的**丢失率** + 是否存在 15s 级卡顿\n"
    "     （api.call 响应丢失会让内核干等 defaultCallTimeout=15s）。\n"
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-build", action="store_true", help="跳过 go/cargo 编译")
    ap.add_argument("--pack", action="store_true", help="追加调试包（_dist）冒烟")
    ap.add_argument("--only", default="", help="只跑指定步骤（逗号分隔）：%s" % ",".join(STEPS))
    a = ap.parse_args()

    only = {s.strip() for s in a.only.split(",") if s.strip()} if a.only else None
    if a.pack and only is None:
        only = None  # --pack 单独追加 pack 步骤
    todo = []
    for s in STEPS:
        if s == "pack" and not a.pack:
            continue
        if s in ("build-go", "test-go", "build-rust") and a.skip_build:
            continue
        if only and s not in only:
            continue
        todo.append(s)

    results = []
    metrics = None
    for s in todo:
        if s == "build-go":
            rc, out, el = run([GO_BIN, "build", "-o", "zcbot_kernel.exe", "."],
                              KERNEL_DIR, 300, tag=s)
            ok = rc == 0
        elif s == "test-go":
            rc, out, el = run([GO_BIN, "test", "./..."], KERNEL_DIR, 300, tag=s)
            ok = rc == 0 and "FAIL" not in out
        elif s == "build-rust":
            rc, out, el = run([CARGO, "build", "--release"], RUST_DIR, 600, tag=s)
            ok = rc == 0
        elif s == "e2e":
            rc, out, el = run([PY, os.path.join(HERE, "e2e_check.py")], PY_SIDE, 300, tag=s)
            ok = rc == 0
        elif s == "official":
            # 真·官方插件（docs 示例 + plugins/llm_blacklist）拖进内核跑
            rc, out, el = run([PY, os.path.join(HERE, "official_plugin_check.py")],
                              PY_SIDE, 300, tag=s)
            ok = rc == 0
        elif s == "surface":
            # 穷举调用 SDK 全部公开 API（插件只报返回值，判定在脚本侧）
            rc, out, el = run([PY, os.path.join(HERE, "api_surface_check.py")],
                              PY_SIDE, 400, tag=s)
            ok = rc == 0
        elif s == "adapter":
            rc, out, el = run([PY, os.path.join(HERE, "adapter_e2e.py")], PY_SIDE, 400, tag=s)
            ok = rc == 0
        elif s == "latency":
            rc, out, el = run([PY, os.path.join(HERE, "latency_probe.py")], PY_SIDE, 180, tag=s)
            ok = rc == 0 and "超时" not in out
        elif s == "stress":
            rc, out, el = run([PY, os.path.join(HERE, "stress_check.py")],
                              PY_SIDE, 600, tag=s)
            ok = rc == 0
            metrics = parse_metrics(out)
        elif s == "pack":
            if not os.path.isdir(DIST):
                print("\n----- [pack] 调试包不存在: %s（跳过）-----" % DIST)
                results.append((s, True, 0.0, "跳过（无 _dist）"))
                continue
            rc, out, el = run([PY, os.path.join("iipc", "tools", "e2e_check.py")],
                              DIST, 300, tag=s)
            ok = rc == 0
        else:
            continue
        results.append((s, ok, el, summarize(s, out)))

    print("\n================ 基准总跑汇总 ================")
    print("%-12s %-6s %-9s %s" % ("步骤", "结果", "耗时", "摘要"))
    for s, ok, el, note in results:
        print("%-12s %-6s %-9s %s" % (s, "PASS" if ok else "FAIL", "%.1fs" % el, note))
    bad = [r for r in results if not r[1]]
    print("----------------------------------------------")
    print("%d 步，通过 %d，失败 %d" % (len(results), len(results) - len(bad), len(bad)))

    if metrics:
        lat, thr = metrics["lat"], metrics["thr"]
        print("\n---- 性能三口径（样本来自 stress 步；端口全部动态分配）----")
        print(METRIC_NOTE.rstrip())
        print("  ① 延迟  P50=%.3fs  P95=%.3fs  P99=%.3fs   样本 %d/%d"
              % (lat["p50"], lat["p95"], lat["p99"], lat["got"], lat["n"]))
        if thr:
            lost = thr["n"] - thr["got"]
            print("  ② 吞吐  %.0f 条/秒   span=%.2fs  样本 %d/%d"
                  % (thr["tps"], thr["span"], thr["got"], thr["n"]))
            print("  ③ 可靠性 丢失 %d/%d（%.2f%%）；突发 span=%.2fs %s"
                  % (lost, thr["n"], 100.0 * lost / max(thr["n"], 1), thr["span"],
                     "（<3s，无 15s 级卡顿）" if thr["span"] < 3.0 else "（⚠ 存在卡顿）"))
        else:
            print("  ② 吞吐  （未抓到，见上方 stress 输出）")
            print("  ③ 可靠性（未抓到，见上方 stress 输出）")
    elif "stress" in todo:
        print("\n---- 性能三口径：stress 输出未抓到指标（格式可能变了）----")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
