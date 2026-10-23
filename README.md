# 数据查看器（Data Viewer）

本地数据查看工具。双击 `启动系统.bat` 即用。

当前版本：**v1.1.0**，详见 [版本记录](CHANGELOG.md)。

## 快速启动

**双击 `启动系统.bat`** —— 弹出终端窗口并自动打开浏览器（http://localhost:8300），**关闭终端窗口即停止服务**。

## 功能

1. 点「选择数据文件」选任意 .xlsx / .xls / **.csv**（CSV 自动识别编码 UTF-8/GBK/UTF-16 与分隔符逗号/Tab/分号/竖线）
2. 自动识别：第一行表头 + 时间列（datetime / unix 秒 / 字符串时间均可）
3. 中间大图显示曲线（**默认不选任何列，在右侧勾选**），底部 K 线式缩放滑块（悬停显示时间）
4. 右侧复选框勾选要显示的列，支持全选/全不选/重置缩放；列名可双击重命名
5. 鼠标悬停显示各曲线数值（十字线，按列名字母序排列）
6. 「导出图片」一键存 PNG；鼠标停在曲线上时按 **Ctrl+Shift+E**，导出包含当前悬浮弹窗和十字线的图片（输入框编辑时不触发）
7. 悬停页面最左侧中间的小按钮，展开历史数据文件列表；点击文件即可恢复数据及界面状态，鼠标移开自动收起。也可用 Tab 聚焦按钮展开、Esc 收起。

**原始数据完整保留**：显示时按当前可见时间范围取样，每组保留首点、末点和最大/最小值，并保留缺测断点。缩放后重新取样，局部点数较少时绘制全部原始点；悬浮读数查询原始数据。底部滑块保留全程概览。

**加载反馈**：加载时页面正中遮罩显示当前状态 + 服务端实时日志（接收→读取→列提取→解析完成，含各阶段耗时），与终端同步。

**会话恢复**：服务未关、网页重开时，弹窗提示"正在恢复上次数据: xxx"，自动恢复数据表及全部状态（选中列/列名/缩放）。

**智能缓存**：首次加载后按文件 SHA-256 缓存解析结果（`data/cache/`），同一文件再次加载免上传免解析。

**界面状态记忆**（`data/uistate/`，按文件哈希一个 JSON）：列名重命名、勾选的曲线集合、时间轴缩放区间——下次打开同一文件自动恢复。

## 文件结构

```
├── 启动系统.bat      # 双击启动（前台终端，关窗即停）
├── server.py         # FastAPI 后端
├── VERSION           # 当前版本号
├── CHANGELOG.md      # 版本记录
├── tests/            # 浏览器回归测试脚本
├── data/             # 运行时数据（全部 gitignore）
│   ├── cache/        # 数据缓存（{sha256}.json）
│   ├── uistate/      # 界面状态（{sha256}.json：列名/选中列/缩放）
│   ├── colmeta.json  # 列含义配置（单位/中文描述）
│   └── session.json  # 最近一次加载的文件（会话恢复用）
└── static/
    └── index.html    # 单页应用
```

## 依赖

```bash
pip install fastapi uvicorn pandas numpy pyarrow openpyxl python-multipart python-calamine
```

## 开发验证

安装测试依赖并运行浏览器回归测试：

```bash
pip install playwright pillow
python -m playwright install chromium
python -m unittest discover -s tests -v
```

测试使用临时数据或合成数据。`tests/` 保存测试脚本；截图 `shots/`、数据缓存 `data/cache/`、界面状态 `data/uistate/`、Python 和测试缓存不提交到 Git。
