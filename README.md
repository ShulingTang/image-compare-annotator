# image-compare-annotator

图片对比标注工具。

## 特性

- 全新深色专业界面（基于 `ui-ux-pro-max` 设计规范重做：Inter 字体、语义化配色、平滑过渡、Toast 反馈、空状态、加载进度条）
- 两种对比模式：**滑块叠加**（可拖动手柄擦除对比、空格翻转）与 **并排对比**（快捷键 `S` 切换）
- **缩放 / 平移**：滚轮缩放（最高 8 倍）、拖拽平移、双击或 `F` 重置，支持像素级核对
- **分组导航**：文件名搜索、按完成状态过滤、点击跳转、`N` 跳到下一个未完成分组
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
