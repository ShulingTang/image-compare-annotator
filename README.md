# image-compare-annotator

图片对比标注工具。

## 特性

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
