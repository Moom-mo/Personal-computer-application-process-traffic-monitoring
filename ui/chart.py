"""历史数据绘图画布。

原实现的绘图逻辑是错误的：

.. code-block:: python

    times.append(r[1])                 # X 轴是字符串 "2026-09-23 10:00:03"
    total_traffic.append(r[7] + r[8])  # Y 轴是进程累计字节数
    axes.plot(times, total_traffic)

三个错误叠加：X 轴传字符串会被 matplotlib 当作**类别轴**按出现顺序排列而不是
按时间；Y 轴用的是累计值（单调递增）；且没有按进程分组，所有进程混在一条线上。
画出来的折线既不对应时间也不对应流量。

这里改为两张各自有明确含义的图：

- 左图：当日**按小时聚合**的流量柱状图
- 右图：当日**按进程聚合**的 Top N 流量横向柱状图
"""
import matplotlib
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from core.utils import MB


def setup_fonts():
    """挑一个系统里真实存在的中文字体，避免图上出现方框。

    原实现直接写死 ``plt.rcParams["font.family"] = ["SimHei"]``，
    系统没装这个字体时所有中文都会渲染成豆腐块。
    """
    from matplotlib import font_manager

    try:
        available = {font.name for font in font_manager.fontManager.ttflist}
    except Exception:
        available = set()
    for name in ("Microsoft YaHei", "SimHei", "SimSun", "DengXian", "Arial Unicode MS"):
        if name in available:
            matplotlib.rcParams["font.family"] = [name]
            break
    matplotlib.rcParams["axes.unicode_minus"] = False


setup_fonts()

# 配色
_COLOR_TREND = "#4c8bf5"
_COLOR_TOP = "#f2994a"


class HistoryCanvas(FigureCanvas):
    """历史分析页的绘图画布，左右两个子图。"""

    def __init__(self, parent=None, width=10.0, height=4.6, dpi=100):
        figure = Figure(figsize=(width, height), dpi=dpi, tight_layout=True)
        super().__init__(figure)
        self.setParent(parent)
        self.ax_trend = figure.add_subplot(1, 2, 1)
        self.ax_top = figure.add_subplot(1, 2, 2)
        self.render_empty("请选择日期后点击「查询并绘图」")

    # ------------------------------------------------------------------ 绘制

    def render_empty(self, message):
        self.ax_trend.clear()
        self.ax_top.clear()
        self._placeholder(self.ax_trend, message)
        self._placeholder(self.ax_top, message)
        self.draw_idle()

    def render(self, day, trend, processes, top_n=8):
        """绘制当日数据。

        :param day: 日期字符串
        :param trend: 长度 24 的列表，每小时的总字节数
        :param processes: 按进程聚合的结果，已按流量降序
        """
        self.ax_trend.clear()
        self.ax_top.clear()

        if not any(trend):
            self._placeholder(self.ax_trend, "当日暂无流量数据")
        else:
            self._draw_trend(day, trend)

        if not processes:
            self._placeholder(self.ax_top, "当日暂无进程数据")
        else:
            self._draw_top(processes, top_n)

        self.draw_idle()

    def _draw_trend(self, day, trend):
        hours = list(range(24))
        values = [value / MB for value in trend]
        self.ax_trend.bar(hours, values, color=_COLOR_TREND, width=0.7)
        self.ax_trend.set_title(f"{day} 分时流量")
        self.ax_trend.set_xlabel("小时")
        self.ax_trend.set_ylabel("流量 (MB)")
        self.ax_trend.set_xticks(range(0, 24, 2))
        self.ax_trend.grid(axis="y", alpha=0.3, linestyle="--")

    def _draw_top(self, processes, top_n):
        top = processes[:top_n]
        names = [p.get("proc_name") or "(未知)" for p in top][::-1]
        values = [(p.get("total") or 0) / MB for p in top][::-1]

        self.ax_top.barh(names, values, color=_COLOR_TOP, height=0.6)
        self.ax_top.set_title(f"进程流量 Top {len(top)}")
        self.ax_top.set_xlabel("流量 (MB)")
        self.ax_top.grid(axis="x", alpha=0.3, linestyle="--")
        # 给每根柱子标数值，数据少时比图例更直观
        for index, value in enumerate(values):
            self.ax_top.text(value, index, f" {value:.2f}", va="center", fontsize=8)

    @staticmethod
    def _placeholder(axes, text):
        axes.text(0.5, 0.5, text, ha="center", va="center",
                  transform=axes.transAxes, color="#888888")
        axes.set_xticks([])
        axes.set_yticks([])
