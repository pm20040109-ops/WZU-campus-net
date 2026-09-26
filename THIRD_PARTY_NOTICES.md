# 第三方材料与授权状态

源码使用 Python 标准库与 Tkinter。独立程序包含 Python、Tcl/Tk 和 PyInstaller 相关组件，发布前需根据实际构建核对并附带适用许可文本。本文件是清单，不代替上游许可证。

界面只选择系统字体，不分发字体文件。图标由 make_icon.py 生成；重建图标需要 Pillow，运行和正常构建无需 Pillow。

## 研究材料

reference 包含门户页面、JS/CSS、配置响应快照；2026-09-20 抓取上下文的分析见 docs/research/。这些文件不参与运行和独立程序打包。

尚未确认每项资源的版本、归属和再分发许可。jquery.i18n、hls、layer、store 等文件名不能证明授权。**因此 reference/ 不随公开仓库分发**（已加入 .gitignore，仅保留在本地）；项目许可证不覆盖任何第三方内容。基于抓取材料的研究笔记（docs/research/）为项目原创内容，随仓库公开。
