"""Agent 验证实验室的隔离应用包。

本包复用正式 Agent、Registry、校验器与业务 Skill，但使用独立入口和独立数据库，
不会向生产 API、生产 PostgreSQL、MinIO 或 SharedContext 写入测试数据。
"""
