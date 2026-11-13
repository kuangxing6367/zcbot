"""指数退避 + 抖动：接入端断线重连的共用策略。

固定间隔重连有两个实际后果：

1. **故障期打得太勤**。服务端下线、网络断了，前端仍按固定节奏猛敲，
   日志被刷满，对端恢复后立刻被一波重连压垮。
2. **多实例同频**。同一批实例重启时按同一个间隔重试，永远同步撞上去，
   形成周期性尖峰（thundering herd）。

退避解决第 1 个问题，抖动解决第 2 个。

关于「什么时候算连上了」：**只在连接存活足够久之后才重置退避**。
如果连上就清零，那么「连上即被踢」的故障（token 失效、被限流、协议不匹配）
会退化成高频重连循环，比固定间隔还糟。判据是存活时长，不是「握手成功」。
"""
import random
import time


class Backoff:
    """一次退避序列。每个接入端持有一个实例，跨重连复用。

    用法::

        self._backoff = Backoff(base=5, maximum=60, jitter=0.25)
        ...
        started = time.monotonic()
        await self._connect_once()
        delay = self._backoff.next_delay(time.monotonic() - started)
        await asyncio.sleep(delay)
    """
    # 【踩坑保佑】可靠性与限流 退避
    # 坑：连上就清零是错的  →  Backoff
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG


    #: 连接存活超过这个秒数才算「稳定」，才重置退避
    DEFAULT_STABLE_AFTER = 30.0

    def __init__(self, base: float = 5.0, maximum: float = 60.0,
                 jitter: float = 0.25,
                 stable_after: float = DEFAULT_STABLE_AFTER):
        self.base = max(1.0, float(base or 5.0))
        # 上限不应小于起始值，否则退避会「越退越快」
        self.maximum = max(self.base, float(maximum or 60.0))
        self.jitter = min(1.0, max(0.0, float(jitter if jitter is not None else 0.25)))
        self.stable_after = float(
            stable_after if stable_after is not None else self.DEFAULT_STABLE_AFTER)
        self.failures = 0
        self._rand = random.Random()

    def reset(self) -> None:
        self.failures = 0

    def next_delay(self, lived_seconds: float = 0.0) -> float:
        """返回本次重连前的等待秒数。

        lived_seconds 是上一次连接从建立到断开持续了多久；达到 stable_after
        视为稳定连接，退避清零。传 0 表示上一次根本没连上。
        """
        if lived_seconds and lived_seconds >= self.stable_after:
            self.failures = 0
        self.failures += 1
        # 第一次失败（failures=1）用 base，之后翻倍
        delay = self.base * (2 ** (self.failures - 1))
        delay = min(delay, self.maximum)
        if self.jitter > 0:
            # 只往上加抖动：不缩短等待，只错开多实例的重试时刻
            delay *= 1.0 + self._rand.uniform(0.0, self.jitter)
        return round(delay, 2)

    def describe(self) -> dict:
        """当前退避状态（供状态页/排错使用）"""
        return {
            'failures': self.failures,
            'base': self.base,
            'maximum': self.maximum,
            'jitter': self.jitter,
            'stable_after': self.stable_after,
        }


def now() -> float:
    """单调时钟，供调用方计算连接存活时长"""
    return time.monotonic()
