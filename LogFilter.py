import sys
import re
import os
import time
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTextEdit, QPushButton,
    QVBoxLayout, QFileDialog, QLabel, QLineEdit, QMessageBox,
    QHBoxLayout, QCheckBox, QListWidget, QSplitter, QProgressBar,
    QComboBox, QMenuBar
)
from PySide6.QtCore import Qt, QEvent, QThread, Signal, QTimer
from PySide6.QtGui import QTextCharFormat, QColor, QTextCursor, QPalette, QAction, QActionGroup
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import mmap
import contextlib


class FilterThread(QThread):
    progress_updated = Signal(int)
    filtering_completed = Signal(list)
    file_processed = Signal(str, list)
    error_occurred = Signal(str)
    partial_results_ready = Signal(list)

    def __init__(self, files, patterns, mode_and, use_mmap=False, chunk_size=1000):
        super().__init__()
        self.files = files
        self.patterns = patterns
        self.mode_and = mode_and
        self.use_mmap = use_mmap
        self._is_running = True
        self.chunk_size = chunk_size
        self.current_file_index = 0
        self.total_files = len(files)

    def run(self):
        try:
            filtered_lines = []
            self.current_file_index = 0
            
            for file in self.files:
                if not self._is_running:
                    break
                
                try:
                    if self.use_mmap:
                        matched = self.filter_file_mmap(file)
                    else:
                        matched = self.filter_file(file)
                    
                    filtered_lines.extend(matched)
                    self.file_processed.emit(file, matched)
                    
                    # 每处理一个文件就更新进度
                    self.current_file_index += 1
                    progress = int((self.current_file_index) / self.total_files * 100)
                    self.progress_updated.emit(progress)
                    
                    # 每收集一定数量的匹配行就发送一次
                    if len(filtered_lines) >= self.chunk_size:
                        self.partial_results_ready.emit(filtered_lines.copy())
                        filtered_lines = []
                        
                except Exception as e:
                    error_msg = f"[读取文件错误 {file}: {e}]"
                    filtered_lines.append(f"{error_msg}\n")
                    self.error_occurred.emit(error_msg)

            # 发送剩余的结果
            if filtered_lines:
                self.partial_results_ready.emit(filtered_lines)
                
            self.filtering_completed.emit(filtered_lines)
        except Exception as e:
            self.error_occurred.emit(f"过滤过程中发生错误: {str(e)}")

    def filter_file(self, file):
        matched = []
        with open(file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not self._is_running:
                    break
                if self.match_line(line):
                    matched.append(line)
        return matched

    def filter_file_mmap(self, file):
        matched = []
        try:
            with open(file, "r", encoding="utf-8", errors="ignore") as f:
                with contextlib.closing(mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)) as m:
                    data = m.read().decode('utf-8', errors='ignore')
                    lines = data.splitlines(True)  # 保留换行符
                    for line in lines:
                        if not self._is_running:
                            break
                        if self.match_line(line):
                            matched.append(line)
        except Exception as e:
            matched.append(f"[读取文件错误 {file}: {e}]\n")
        return matched

    def match_line(self, line):
        if self.mode_and:
            return all(p.search(line) for p in self.patterns)
        else:
            return any(p.search(line) for p in self.patterns)

    def stop(self):
        self._is_running = False
        self.wait()


class LogFilterApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("日志关键词过滤工具")
        self.setMinimumSize(900, 600)

        self.selected_files = []
        self.filtered_lines = []
        self.filter_thread = None
        self.filter_start_time = 0
        self.displayed_text = ""
        
        # 主题检测
        self.is_dark_theme = self.detect_dark_theme()
        self.current_theme = "system" # 默认跟随系统主题

        self.init_ui()
        self.setup_drag_drop()
        self.apply_theme()

    def detect_dark_theme(self):
        """检测当前应用是否使用深色主题"""
        palette = self.palette()
        # 通过对比前景色和背景色的亮度来判断主题
        bg_brightness = palette.color(QPalette.Window).lightness()
        text_brightness = palette.color(QPalette.WindowText).lightness()
        return text_brightness > bg_brightness

    def init_ui(self):
        # 创建主分割器
        main_splitter = QSplitter(Qt.Vertical)
        self.setCentralWidget(main_splitter)

        # 顶部文件区域
        file_area = QWidget()
        file_layout = QVBoxLayout(file_area)
        file_layout.setContentsMargins(5, 5, 5, 5)

        # 文件列表
        self.file_list_widget = QListWidget()
        self.file_list_widget.setToolTip("已加载的日志文件列表")
        file_layout.addWidget(self.file_list_widget)

        # 搜索控制区
        search_layout = QHBoxLayout()

        self.keyword_input = QLineEdit()
        self.keyword_input.setPlaceholderText("输入关键词（多个关键词用英文逗号分隔）")
        search_layout.addWidget(QLabel("关键词："))
        search_layout.addWidget(self.keyword_input)

        self.case_checkbox = QCheckBox("区分大小写")
        search_layout.addWidget(self.case_checkbox)

        self.wholeword_checkbox = QCheckBox("全字匹配")
        search_layout.addWidget(self.wholeword_checkbox)

        self.and_or_button = QPushButton("模式: 或")
        self.and_or_button.setCheckable(True)
        self.and_or_button.clicked.connect(self.toggle_and_or_mode)
        search_layout.addWidget(self.and_or_button)

        # 新增：在创建菜单栏之前初始化 use_mmap_checkbox
        self.use_mmap_checkbox = QCheckBox("使用内存映射加速")
        self.use_mmap_checkbox.setChecked(True)
        
        # 创建菜单栏 - 现在 use_mmap_checkbox 已经定义
        self.create_menu_bar()

        # 增强过滤按钮的视觉效果
        self.filter_button = QPushButton("执行过滤")
        self.filter_button.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                font-weight: bold;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
        """)
        self.filter_button.setMinimumHeight(30)
        self.filter_button.clicked.connect(self.filter_logs)
        search_layout.addWidget(self.filter_button)

        file_layout.addLayout(search_layout)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.hide()
        file_layout.addWidget(self.progress_bar)

        # 日志预览
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)

        # 保存按钮
        self.save_button = QPushButton("另存为 debug.log")
        self.save_button.clicked.connect(self.save_debug_log)

        # 添加到分割器
        main_splitter.addWidget(file_area)
        main_splitter.addWidget(self.preview)
        main_splitter.addWidget(self.save_button)

        # 设置分割器比例
        main_splitter.setSizes([150, 400, 30])

    def create_menu_bar(self):
        menu_bar = self.menuBar()

        # 设置菜单
        # settings_menu = menu_bar.addMenu("设置")

        # 主题子菜单
        theme_menu = menu_bar.addMenu("主题")
        theme_group = QActionGroup(self)
        
        light_theme_action = QAction("浅色主题", self, checkable=True)
        light_theme_action.setActionGroup(theme_group)
        light_theme_action.triggered.connect(lambda: self.change_theme("浅色主题"))
        theme_menu.addAction(light_theme_action)
        
        dark_theme_action = QAction("深色主题", self, checkable=True)
        dark_theme_action.setActionGroup(theme_group)
        dark_theme_action.triggered.connect(lambda: self.change_theme("深色主题"))
        theme_menu.addAction(dark_theme_action)
        
        system_theme_action = QAction("跟随系统", self, checkable=True)
        system_theme_action.setActionGroup(theme_group)
        system_theme_action.triggered.connect(lambda: self.change_theme("跟随系统"))
        theme_menu.addAction(system_theme_action)
        
        # 初始化主题选择
        if self.current_theme == "light":
            light_theme_action.setChecked(True)
        elif self.current_theme == "dark":
            dark_theme_action.setChecked(True)
        else:
            system_theme_action.setChecked(True)

        # 性能选项
        performance_menu = menu_bar.addMenu("性能")
        
        self.memory_mapping_action = QAction("使用内存映射加速", self, checkable=True)
        self.memory_mapping_action.setChecked(self.use_mmap_checkbox.isChecked())
        self.memory_mapping_action.triggered.connect(self.toggle_memory_mapping)
        performance_menu.addAction(self.memory_mapping_action)

    def toggle_memory_mapping(self):
        self.use_mmap_checkbox.setChecked(self.memory_mapping_action.isChecked())

    def setup_drag_drop(self):
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        files = [url.toLocalFile() for url in urls if url.toLocalFile().endswith((".log", ".txt"))]
        if files:
            self.load_files(files)

    def load_files(self, file_list):
        self.selected_files = file_list
        self.file_list_widget.clear()
        self.file_list_widget.addItems(file_list)
        self.statusBar().showMessage(f"已加载 {len(file_list)} 个文件")

    def toggle_and_or_mode(self):
        if self.and_or_button.isChecked():
            self.and_or_button.setText("模式: 与")
        else:
            self.and_or_button.setText("模式: 或")

    def build_keyword_patterns(self, keywords):
        case_sensitive = self.case_checkbox.isChecked()
        whole_word = self.wholeword_checkbox.isChecked()
        flags = 0 if case_sensitive else re.IGNORECASE

        patterns = []
        for k in keywords:
            p = r'\b{}\b'.format(re.escape(k)) if whole_word else re.escape(k)
            patterns.append(re.compile(p, flags))

        return patterns

    def change_theme(self, theme_text):
        if theme_text == "深色主题":
            self.current_theme = "dark"
        elif theme_text == "浅色主题":
            self.current_theme = "light"
        else:  # 跟随系统
            self.current_theme = "system"
            # 检测当前系统主题
            self.is_dark_theme = self.detect_dark_theme()
            
        self.apply_theme()
        
        # 如果过滤结果已经显示，重新应用高亮
        if self.filtered_lines:
            self.highlight_filtered_text(self.filtered_lines, self.keyword_input.text().split(","))

    def apply_theme(self):
        palette = self.palette()
        
        if self.current_theme == "dark":
            # 深色主题配色
            palette.setColor(QPalette.Window, QColor(53, 53, 53))
            palette.setColor(QPalette.WindowText, Qt.white)
            palette.setColor(QPalette.Base, QColor(25, 25, 25))
            palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
            palette.setColor(QPalette.ToolTipBase, Qt.white)
            palette.setColor(QPalette.ToolTipText, Qt.white)
            palette.setColor(QPalette.Text, Qt.white)
            palette.setColor(QPalette.Button, QColor(53, 53, 53))
            palette.setColor(QPalette.ButtonText, Qt.white)
            palette.setColor(QPalette.BrightText, Qt.red)
            palette.setColor(QPalette.Link, QColor(42, 130, 218))
            palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
            palette.setColor(QPalette.HighlightedText, Qt.black)
        elif self.current_theme == "light":
            # 浅色主题配色 - 使用自定义浅色方案
            palette.setColor(QPalette.Window, QColor(240, 240, 240))
            palette.setColor(QPalette.WindowText, Qt.black)
            palette.setColor(QPalette.Base, QColor(255, 255, 255))
            palette.setColor(QPalette.AlternateBase, QColor(245, 245, 245))
            palette.setColor(QPalette.ToolTipBase, Qt.black)
            palette.setColor(QPalette.ToolTipText, Qt.black)
            palette.setColor(QPalette.Text, Qt.black)
            palette.setColor(QPalette.Button, QColor(240, 240, 240))
            palette.setColor(QPalette.ButtonText, Qt.black)
            palette.setColor(QPalette.BrightText, Qt.red)
            palette.setColor(QPalette.Link, QColor(0, 0, 255))
            palette.setColor(QPalette.Highlight, QColor(170, 200, 255))
            palette.setColor(QPalette.HighlightedText, Qt.black)
        else:  # 跟随系统
            if self.is_dark_theme:
                # 使用深色主题配置
                palette.setColor(QPalette.Window, QColor(53, 53, 53))
                palette.setColor(QPalette.WindowText, Qt.white)
                palette.setColor(QPalette.Base, QColor(25, 25, 25))
                palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
                palette.setColor(QPalette.ToolTipBase, Qt.white)
                palette.setColor(QPalette.ToolTipText, Qt.white)
                palette.setColor(QPalette.Text, Qt.white)
                palette.setColor(QPalette.Button, QColor(53, 53, 53))
                palette.setColor(QPalette.ButtonText, Qt.white)
                palette.setColor(QPalette.BrightText, Qt.red)
                palette.setColor(QPalette.Link, QColor(42, 130, 218))
                palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
                palette.setColor(QPalette.HighlightedText, Qt.black)
            else:
                # 使用浅色主题配置
                palette.setColor(QPalette.Window, QColor(240, 240, 240))
                palette.setColor(QPalette.WindowText, Qt.black)
                palette.setColor(QPalette.Base, QColor(255, 255, 255))
                palette.setColor(QPalette.AlternateBase, QColor(245, 245, 245))
                palette.setColor(QPalette.ToolTipBase, Qt.black)
                palette.setColor(QPalette.ToolTipText, Qt.black)
                palette.setColor(QPalette.Text, Qt.black)
                palette.setColor(QPalette.Button, QColor(240, 240, 240))
                palette.setColor(QPalette.ButtonText, Qt.black)
                palette.setColor(QPalette.BrightText, Qt.red)
                palette.setColor(QPalette.Link, QColor(0, 0, 255))
                palette.setColor(QPalette.Highlight, QColor(170, 200, 255))
                palette.setColor(QPalette.HighlightedText, Qt.black)
            
        self.setPalette(palette)
        
        # 为QTextEdit设置特殊样式
        if self.current_theme == "dark" or (self.current_theme == "system" and self.is_dark_theme):
            self.preview.setStyleSheet("QTextEdit { background-color: #191919; color: white; }")
        else:
            self.preview.setStyleSheet("QTextEdit { background-color: #ffffff; color: black; }")

    def filter_logs(self):
        if not self.selected_files:
            QMessageBox.warning(self, "未选择日志文件", "请先拖拽日志文件到窗口中。")
            return

        keyword_text = self.keyword_input.text().strip()
        if not keyword_text:
            QMessageBox.warning(self, "未输入关键词", "请填写关键词后再执行过滤。")
            return

        keywords = [k.strip() for k in keyword_text.split(",") if k.strip()]
        if not keywords:
            QMessageBox.warning(self, "无有效关键词", "请填写有效关键词。")
            return

        patterns = self.build_keyword_patterns(keywords)
        mode_and = self.and_or_button.isChecked()
        use_mmap = self.use_mmap_checkbox.isChecked()

        self.filtered_lines.clear()
        self.displayed_text = ""
        self.preview.clear()
        self.preview.setPlainText("开始过滤...")

        # 禁用过滤按钮
        self.filter_button.setEnabled(False)
        self.filter_button.setText("过滤中...")
        self.progress_bar.setValue(0)
        self.progress_bar.show()

        # 启动过滤线程
        self.filter_thread = FilterThread(self.selected_files, patterns, mode_and, use_mmap)
        self.filter_thread.progress_updated.connect(self.update_progress)
        self.filter_thread.filtering_completed.connect(self.on_filtering_completed)
        self.filter_thread.file_processed.connect(self.on_file_processed)
        self.filter_thread.error_occurred.connect(self.on_error_occurred)
        self.filter_thread.partial_results_ready.connect(self.on_partial_results_ready)
        self.filter_start_time = time.time()
        self.filter_thread.start()

    def update_progress(self, value):
        self.progress_bar.setValue(value)

    def on_file_processed(self, file_name, lines):
        file_name = os.path.basename(file_name)
        self.statusBar().showMessage(f"处理文件: {file_name} - 找到 {len(lines)} 匹配行")

    def on_error_occurred(self, error_msg):
        self.statusBar().showMessage(error_msg)
        self.preview.append(error_msg)

    def on_partial_results_ready(self, lines):
        # 累加结果
        self.filtered_lines.extend(lines)
        
        # 追加到显示文本
        new_text = "".join(lines)
        self.displayed_text += new_text
        
        # 定期更新UI，避免长时间阻塞
        if len(self.displayed_text) > 10000 or not self.filter_thread.isRunning():
            self.preview.setPlainText(self.displayed_text)
            # 滚动到底部
            self.preview.moveCursor(QTextCursor.End)
            # 处理UI事件
            QApplication.processEvents()

    def on_filtering_completed(self, lines):
        # 累加最后一批结果
        self.filtered_lines.extend(lines)
        self.displayed_text += "".join(lines)
        
        # 更新UI
        self.preview.setPlainText(self.displayed_text)
        self.preview.moveCursor(QTextCursor.End)
        
        elapsed_time = time.time() - self.filter_start_time

        # 恢复UI状态
        self.filter_button.setEnabled(True)
        self.filter_button.setText("执行过滤")
        self.progress_bar.hide()

        if self.filtered_lines:
            self.highlight_filtered_text(self.filtered_lines, self.keyword_input.text().split(","))
            self.statusBar().showMessage(f"过滤完成 - 找到 {len(self.filtered_lines)} 匹配行 - 耗时: {elapsed_time:.2f}秒")
        else:
            self.preview.setPlainText("[无匹配结果]")
            self.statusBar().showMessage(f"过滤完成 - 无匹配结果 - 耗时: {elapsed_time:.2f}秒")

    def highlight_filtered_text(self, lines, keywords):
        # 清除现有格式
        self.preview.setPlainText("".join(lines))
        
        # 根据主题选择高亮颜色
        if self.current_theme == "dark":
            # 深色主题高亮颜色
            keyword_bg_color = QColor("#404080")  # 深蓝色背景
            level_colors = {
                "[d]": QColor("#bbbbbb"),  # Debug - 浅灰色
                "[i]": QColor("#80ff80"),  # Info - 浅绿色
                "[w]": QColor("#ffcc66"),  # Warning - 浅黄色
                "[e]": QColor("#ff9999"),  # Error - 浅红色
                "[f]": QColor("#ff99ff"),  # Fatal - 浅洋红
            }
        else:
            # 浅色主题高亮颜色
            keyword_bg_color = QColor("yellow")  # 黄色背景
            level_colors = {
                "[d]": QColor("#999999"),  # Debug - 灰色
                "[i]": QColor("#008000"),  # Info - 绿色
                "[w]": QColor("#e68a00"),  # Warning - 橙色
                "[e]": QColor("#cc0000"),  # Error - 红色
                "[f]": QColor("#ff00ff"),  # Fatal - 洋红
            }

        # 基本关键词高亮
        fmt = QTextCharFormat()
        fmt.setBackground(keyword_bg_color)

        case_sensitive = self.case_checkbox.isChecked()
        flags = 0 if case_sensitive else re.IGNORECASE

        cursor = self.preview.textCursor()
        document = self.preview.document()
        
        # 先收集所有匹配位置
        matches = []
        text = "".join(lines)
        
        for kw in keywords:
            kw = kw.strip()
            if not kw:
                continue
            pattern = re.compile(re.escape(kw), flags)
            for match in pattern.finditer(text):
                matches.append((match.start(), match.end()))
        
        # 按起始位置排序
        matches.sort(key=lambda x: x[0])
        
        # 应用高亮，分批处理以避免UI阻塞
        batch_size = 50
        for i in range(0, len(matches), batch_size):
            batch = matches[i:i+batch_size]
            for start, end in batch:
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.KeepAnchor)
                cursor.mergeCharFormat(fmt)
            # 处理UI事件
            QApplication.processEvents()

        # 日志等级颜色高亮
        for level, color in level_colors.items():
            level_pattern = re.compile(re.escape(level))
            fmt = QTextCharFormat()
            fmt.setForeground(color)
            
            level_matches = []
            for match in level_pattern.finditer(text):
                level_matches.append((match.start(), match.end()))
            
            for i in range(0, len(level_matches), batch_size):
                batch = level_matches[i:i+batch_size]
                for start, end in batch:
                    cursor.setPosition(start)
                    cursor.setPosition(end, QTextCursor.KeepAnchor)
                    cursor.mergeCharFormat(fmt)
                # 处理UI事件
                QApplication.processEvents()

    def save_debug_log(self):
        if not self.filtered_lines:
            QMessageBox.information(self, "无内容", "没有可保存的过滤内容。")
            return

        filename, _ = QFileDialog.getSaveFileName(self, "保存日志", "debug.log", "Log Files (*.log)")
        if filename:
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    f.writelines(self.filtered_lines)
                QMessageBox.information(self, "保存成功", f"日志已保存为 {filename}")
            except Exception as e:
                QMessageBox.critical(self, "保存失败", f"无法保存文件：{e}")

    def closeEvent(self, event):
        if self.filter_thread and self.filter_thread.isRunning():
            self.filter_thread.stop()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    # 设置样式以确保主题生效
    app.setStyle("Fusion")
    window = LogFilterApp()
    window.show()
    sys.exit(app.exec())    