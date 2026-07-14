# 运行界面

`run_ui` 是项目根目录下的动态命令行界面，完全使用 Python 标准库。

```powershell
python run.py
```

启动后可连续输入任务；执行期间会显示状态动画，结束后展示回答和工具调用轨迹。常用命令：

- `/help`：查看帮助
- `/exit` 或 `/quit`：结束会话

也可一次性执行任务：

```powershell
python run.py "计算 (25 + 17) * 3"
```
