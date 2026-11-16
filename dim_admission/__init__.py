"""维度事实准入器（XBRL 风格子集，非完整 XBRL 实现）。

模块划分：
- store:    SQLite 存储、建表、JSON 装载
- precheck: 分类包/上下文预检（结构错误与裁定错误分离）
- network:  带 (概念, 角色) 状态的图遍历、闭包编译与缓存
- engine:   cube 检查、角色组真值裁定、批量事实准入
- explain:  逐项正反依据的文本解释
- cli:      命令行入口
"""

__all__ = ["store", "precheck", "network", "engine", "explain", "cli"]
