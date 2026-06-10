# image-compare-annotator

图片对比标注工具。

## 特性

- 全新深色专业界面（基于 `ui-ux-pro-max` 设计规范重做：Inter 字体、语义化配色、平滑过渡、Toast 反馈、空状态、加载进度条）
- 三种对比模式：**滑块叠加**（拖动手柄擦除、空格翻转）、**并排对比**、**像素差异高亮**（浏览器端逐像素计算、可调灵敏度），`S` 键循环切换
- **缩放 / 平移 / 放大镜**：滚轮缩放（最高 8 倍）、拖拽平移、双击或 `F` 重置、`L` 开关局部放大镜，支持像素级核对
- **撤销**：`U` / `Ctrl·⌘ + Z` 逐步回退最近 50 步标注
- **分组导航**：文件名搜索、按完成状态过滤、点击跳转、`N` 跳到下一个未完成分组
- **导出与分拣**：合格 / 不合格名单（txt）、汇总 JSON、明细 CSV，以及一键把效果图按结果复制到 `qualified` / `unqualified` 目录
- 内置快捷键帮助面板（`?`）
- 提供 macOS `.app` 启动包：`ImageCompareAnnotator.app`
- 补充 favicon，去掉浏览器图标 404
- 补充详细使用文档：`USAGE.md`

## 启动方式

### Ubuntu / Linux

```bash
chmod +x start.sh
./start.sh
```

### macOS

优先双击：
- `ImageCompareAnnotator.app`

备选：
- `start.command`

### Windows

双击：
- `start.bat`

## 环境变量

- `ANNOTATOR_PORT`：期望启动端口，默认 `8765`
- `ANNOTATOR_AUTO_OPEN`：是否自动打开浏览器，默认 `1`

## 文档

- 使用说明：`USAGE.md`
