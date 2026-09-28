"""查询结果导出。

用标准库 csv 而不是 pandas：导出的数据量不大，标准库零依赖、启动更快，
而且用 ``utf-8-sig`` 编码写出的 CSV 在 Excel 里双击打开不会中文乱码。
"""
import csv
import os
import time


def export_rows(path, headers, rows):
    """把二维数据写成 CSV，返回写入的绝对路径。"""
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.writer(fp)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def default_csv_name(prefix, day):
    """生成默认文件名，例如 traffic_2026-09-23_120000.csv。"""
    return f"{prefix}_{day}_{time.strftime('%H%M%S')}.csv"
