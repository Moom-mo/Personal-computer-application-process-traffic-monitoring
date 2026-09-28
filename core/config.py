"""配置加载。

读取项目根目录下的 config.json，缺失或损坏的配置项自动回落到默认值，
避免因为改坏配置文件导致整个服务起不来。
"""
import copy
import json
import os

# core/config.py -> core -> 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CONFIG = {
    "server": {
        "host": "127.0.0.1",
        "port": 8899,
        # 客户端等待服务端响应的超时（秒）
        "timeout": 5.0,
    },
    "collect": {
        # 采集周期（秒）
        "interval": 3,
        # 单次增量小于该值不写库，用于压制噪声、控制数据量
        "min_delta_bytes": 1,
        # 把采集服务自身排除在统计之外，避免监控工具自己写库的开销
        # 被当成"网络流量"而常年占据流量榜首
        "exclude_self": True,
        # 连接快照的最短写入间隔（秒），避免连接频繁变动时刷爆数据库
        "conn_snapshot_min_gap": 30,
        # 连接集合长时间不变时的心跳写入间隔（秒）
        "conn_snapshot_heartbeat": 900,
    },
    "database": {
        "file": "traffic_monitor.db",
        # 历史数据保留天数，超期自动清理
        "retention_days": 14,
    },
    "quota": {
        # 告警静默期（秒），同一进程在该时间内只告警一次
        "alert_cooldown": 60,
        # 超配额动作：alert = 仅告警（默认）；suspend = 告警并挂起进程
        "alert_action": "alert",
    },
    "log": {
        "level": "INFO",
        "dir": "logs",
    },
}


def _merge(base, override):
    """递归合并字典，override 中出现的键覆盖 base。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path=None):
    """加载配置，返回合并后的字典。"""
    path = path or os.path.join(PROJECT_ROOT, "config.json")
    user_config = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                user_config = json.load(fp)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[config] 读取 {path} 失败，改用默认配置：{exc}")
    config = _merge(DEFAULT_CONFIG, user_config)
    config["_project_root"] = PROJECT_ROOT
    return config


def resolve_path(config, *parts):
    """把配置中的相对路径解析为相对项目根目录的绝对路径。"""
    joined = os.path.join(*parts)
    if os.path.isabs(joined):
        return joined
    return os.path.join(config["_project_root"], joined)
