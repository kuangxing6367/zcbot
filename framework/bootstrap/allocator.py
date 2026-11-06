"""内存分配器自动策略。

必须在 import 重型 framework 之前执行：PYTHONMALLOC 是解释器启动前就定死的，
运行时改不了；所以此函数在最早期根据 config 的 memory.allocator 决定分配器，
并 os.execv 重启自身一次性套用（带 ZCBOT_ALLOC_APPLIED 防重入标记，避免无限重启）。

allocator 三态：
  auto     （默认）生产 Linux 自动用 malloc（pymalloc arena 残留导致 RSS 地板，
            且 malloc_trim 在 Linux 最有效）；Windows/macOS 开发环境保持 pymalloc
            （SetProcessWorkingSetSize 频繁换页会影响体验）。
  malloc   强制 malloc（追求最低内存地板，小对象分配略慢）。
  pymalloc 强制 pymalloc（默认行为，最高分配吞吐）。
"""
import os
import sys


def apply_allocator_policy():
    if os.environ.get('ZCBOT_ALLOC_APPLIED'):
        return
    alloc = 'auto'
    try:
        import yaml
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', 'config.yaml')
        cfg_path = os.path.abspath(cfg_path)
        if os.path.isfile(cfg_path):
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
            alloc = (cfg.get('memory') or {}).get('allocator', 'auto')
    except Exception:
        alloc = 'auto'
    alloc = str(alloc).strip().lower()
    if alloc == 'auto':
        alloc = 'malloc' if sys.platform == 'linux' else 'pymalloc'
    if alloc == 'malloc' and os.environ.get('PYTHONMALLOC') != 'malloc':
        os.environ['PYTHONMALLOC'] = 'malloc'
        os.environ['ZCBOT_ALLOC_APPLIED'] = '1'
        # flush=True 必需：紧接着就 os.execv 换掉整个进程，
        # 缓冲未刷出时这行提示会直接丢失（生产上看不到策略生效）。
        print('[内存策略] 已自动以 PYTHONMALLOC=malloc 重启进程'
              '（追求更低内存地板；小对象分配略慢，详见日志）', flush=True)
        os.execv(sys.executable, [sys.executable] + sys.argv)
