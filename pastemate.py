#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PasteMate —— Windows 桌面悬浮“粘贴助手”工具条
==============================================

一、核心交互
    * 启动后没有主窗口，只在系统托盘驻留一个图标；
    * 任意界面按下全局热键 Ctrl + Shift + F，
      在鼠标光标附近弹出一个无边框极简工具条；
    * 工具条失去焦点（点击别处）时自动关闭。

二、工具条布局（两行式：上面输入，下面预览）
    ┌──────────────── 输入框（占满） ─────────────────┬─ 📋 ─┐
    │ 粘贴 / 输入原始文本                              │ 复制 │
    ├─────────────────────────────────────────────────┴──────┤
    │ 预览面板：实时处理结果（多行、自动换行、可滚轮滚动）      │
    ├────────────────────────────────────────────────────────┤
    │ ✅ 小字提示当前命中的识别规则（如 “✅ 检测为 JSON”）      │
    └────────────────────────────────────────────────────────┘

    * 预览面板会随内容长高（最多 8 行，超出可滚轮查看），
      空输入时收回到最小高度；窗口整体高度因此是自适应的；
    * 点击 “📋”（或在输入框按 Enter）：把预览结果写入系统剪贴板，
      下方闪现 “已复制” 提示，1.5 秒后整个工具条自动关闭；
    * 在输入框按 Esc：不复制，直接关闭。

    说明：预览面板和输入框用的都是 tk.Text，不是 tk.Entry。
      * tk.Entry 会把换行符渲染成零宽字符 —— json.dumps(indent=2) 的
        多行结果在预览里被压成一行，用户没法在复制前确认内容；
      * 更要命的是 tk.Entry 处理超长字符串是 O(n²) 级的：实测插入
        8000 字符要 1.2 秒、16000 字符要 4.9 秒（同样操作 tk.Text 只要
        0.0 ms）。粘贴一大段 JSON 时 Entry 会把主线程卡死几十秒，
        Windows 判定“未响应”，看起来就像程序崩溃。v1.4.3 起换成 Text。
      * 预览面板最多渲染前 PREVIEW_MAX_CHARS(20000) 个字符，超出部分
        不在预览里显示（并会明确提示），但“复制”始终用完整结果。

三、识别优先级（依次尝试，失败即落到下一条）
    JSON > 时间戳(10位秒/13位毫秒) > URL编码 > Base64 > 纯文本清理

    注意（v1.4.2 修复）：Base64 规则额外要求“解码出来的文本至少 4 个字符”。
    否则 file / edit / byte / bits 这类普通英文单词会被当成 Base64 解码成
    "~)^"、"yح" 这样的乱码，把用户粘进去的原文悄悄改掉。门槛是按语料量
    出来的，详见 B64_MIN_TEXT_LEN 附近的注释。

四、单实例逻辑
    通过绑定本机回环 UDP 端口实现“互斥锁 + 唤醒通道”：
    重复启动时，新进程先与占用者握手（PMATE? -> PMATE!）确认对方
    确实是 PasteMate，再发送 "SHOW" 唤醒并自行退出；若端口被其它
    程序占用则弹窗提示用户换端口（环境变量 PASTEMATE_PORT）。
    若端口落在 Windows/Hyper-V 动态保留段内（无进程持有但 bind 失败），
    会自动换用相邻的候选端口（port+1 / port+2）。
    老进程收到消息后弹出工具条 —— 效果等同“再次运行即唤起已有实例”。

五、热键实现说明（重要）
    采用“双通道”监听，互为备份，任一通道触发都会弹出工具条：
      * 主通道 pynput：Windows 低级键盘钩子，普通权限即可工作；
        （不选 keyboard 库的原因：它在部分场景下必须“以管理员身份运行”
        才能收到按键，否则热键静默失效）
      * 备用通道 Win32 RegisterHotKey：系统原生热键注册，不依赖钩子，
        即使安全软件拦截了钩子也依然有效。
        注意：该组合键会被系统占用（吞掉），PasteMate 运行期间其它
        程序将收不到 Ctrl+Shift+F；若需要换键请修改下方 HOTKEY。
    两通道同时触发时由 0.3 秒去抖窗口保证只弹一次。

六、依赖（见 requirements.txt）
    pynput / pyperclip / pystray / Pillow；tkinter 为 Python 标准库。

七、自定义
    * 改热键：修改下方 HOTKEY 字符串（pynput 语法，如 '<ctrl>+<alt>+v'，
      或具名键 '<ctrl>+<shift>+<f5>' / '<ctrl>+<alt>+<home>'）；
    * 改端口：设环境变量 PASTEMATE_PORT，或直接改 SINGLETON_PORT 常量；
    * 纯文本清理默认会把连续空格压缩为一个（代码对齐用的空格会被改动），
      如需保留原始空格，注释掉 process_text() 规则5中的空格压缩一行即可；
    * 开机自启：托盘右键菜单勾选“开机自启”即可（写入 HKCU Run 注册表项，
      无需管理员权限；程序每次启动会自动刷新注册表中的路径，移动目录不失效）。
"""

import base64
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime

# ---------------- 第三方依赖（缺失时给出友好提示并退出） ----------------
try:
    import pyperclip          # 剪贴板读写
    from pynput import keyboard   # 全局热键（选型理由见模块 docstring 第五节）
    import pystray            # 系统托盘图标
    from PIL import Image, ImageDraw  # pystray 需要的图像库，用于程序内绘制图标
except ImportError as _e:
    _msg = ('PasteMate 缺少依赖：{}\n\n请先在本目录执行：\n'
            '    pip install -r requirements.txt').format(_e)
    try:
        print(_msg)
    except Exception:
        pass  # pythonw 下没有控制台，打印失败也无妨
    try:
        # 用系统原生弹窗提示，不引入任何第三方 GUI 依赖
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, _msg, 'PasteMate 启动失败', 0x10)
    except Exception:
        pass
    sys.exit(1)

# tkinter 是标准库，单独导入（极少数精简版 Python 可能没有）
import tkinter as tk

# winreg 用于读写“开机自启”注册表项（仅 Windows 存在，缺失时自动禁用该功能）
try:
    import winreg
except ImportError:
    winreg = None


# ============================================================================
# 常量配置
# ============================================================================

APP_NAME = 'PasteMate'
APP_VERSION = '1.4.3'   # 版本标记：托盘悬停提示/日志中可见，用于确认新版本已在运行

# 全局热键，pynput 语法。想换热键改这里即可，例如 '<ctrl>+<alt>+v'
HOTKEY = '<ctrl>+<shift>+f'

# 单实例互斥 + 进程间唤醒使用的本机回环端口（可用环境变量 PASTEMATE_PORT 覆盖）。
# 注意：Windows/Hyper-V 会动态保留一些 UDP 端口段（netsh interface ipv4
# show excludedportrange protocol=udp 可查），落在保留段内的端口 bind 会失败。
# 因此实际会依次尝试 port / port+1 / port+2 三个候选端口。
SINGLETON_PORT = int(os.environ.get('PASTEMATE_PORT') or '53177')

# 主循环轮询间隔（毫秒）：处理跨线程任务队列与单实例唤醒消息
POLL_MS = 100

# 调试开关：设置环境变量 PASTEMATE_DEBUG=1 可在前台抢占时输出诊断日志
DEBUG = os.environ.get('PASTEMATE_DEBUG') == '1'

# ---- Base64 识别的“防误判”门槛（见 规则4）----
# 背景：file / edit / byte / bits / also / city / fill / fish 这类普通英文单词
# 恰好满足 Base64 字符集与长度要求，解码后得到 "~)^"、"j[(" 这样的可打印短串。
# 旧版只要求“可打印”，于是把单词静默解码成乱码写进剪贴板。
#
# 门槛是量出来的，不是拍脑袋（2026-10-16，语料 = 497 个英文常用词 + 120 个常见
# 标识符 + 42 个驼峰命名 + 12000 个随机串；真样本 = 24 个真实用途的 base64）：
#   真正的误判样本解码后全都只有 2~3 个字符（"~)^" / "yح" / "o+^" / "n+l" /
#   "j[(" / "r+r" / "~)e" / "~+!"）；而真 base64 解出的文本都 >= 4 字符。
#   “解码结果 >= 4 字符”这一条即可做到：真实词误判 0 个、真样本漏检 0 个。
#   对比：再加 alnum（字母数字占比）门槛反而会漏掉 eyJhIjogMX0= → {"a": 1}
#   （alnum 仅 0.25），所以不采用。
B64_MIN_LEN = 4        # 输入至少 4 字符才当候选（与原行为一致）
B64_MIN_TEXT_LEN = 4   # 解码结果至少 4 字符，否则判为乱码、不认（关键防线）

# 工具条尺寸与布局（像素）
# 布局：第一行 = 输入框（占满到按钮之前）+ 📋 按钮；第二行 = 全宽预览面板
#       （高度按内容行数自适应）；第三行 = 规则小字；最后是“已复制”提示行。
# 早期版本把预览塞进第一行右侧 30% 的窄槽（约 208px ≈ 17 个 ASCII 字符），
# 且 tk.Entry 会把换行渲染成零宽字符 —— 结果格式化后的 JSON 完全无法预览。
POPUP_W = 720
CTRL_Y, CTRL_H = 10, 40        # 第一行控件（输入框 / 按钮）的纵坐标与高度
PAD, GAP = 10, 6               # 外边距与控件间距
PREVIEW_MAX_LINES = 8          # 预览面板最多显示多少行（超出部分可滚轮滚动）
PREVIEW_MIN_H = 30             # 预览面板最小高度（一行文字 + 内边距）
# 预览面板最多渲染多少字符。超长内容只渲染前面这一段（否则 Text 控件自身会
# 变慢），但“复制”始终使用完整的处理结果，不会被截断。
PREVIEW_MAX_CHARS = 20000

# 配色方案
BG            = '#FFFFFF'      # 工具条背景
FIELD_BG      = '#F6F8FA'      # 输入框 / 预览框背景
FG_MAIN       = '#24292F'      # 正常文字
FG_PLACEHOLDER= '#8B949E'      # 占位 / 弱化文字
BORDER        = '#C9CDD4'      # 边框
ACCENT        = '#2563EB'      # 主题蓝（按钮、焦点框）
FAIL_FG       = '#CF222E'      # 处理失败时的红色文字
RULE_OK_FG    = '#1A7F37'      # 规则命中提示（绿）
RULE_FAIL_FG  = '#CF222E'      # 规则失败提示（红）
TOAST_FG      = '#1A7F37'      # “已复制”提示文字色

TOAST_MS = 1500                # “已复制”提示停留时长（毫秒），规格要求 1.5 秒

# 中文字体（Windows 自带）；若想换字体改这里
FONT_UI    = ('Microsoft YaHei UI', 11)
FONT_SMALL = ('Microsoft YaHei UI', 9)
FONT_BTN   = ('Segoe UI Emoji', 15)


def log(*args):
    """带前缀的安全打印：pythonw 运行时没有 stdout，静默忽略即可。"""
    try:
        print('[PasteMate]', *args, flush=True)
    except Exception:
        pass


def tb():
    """
    安全打印异常堆栈。
    注意：pythonw 运行时 sys.stderr 是 None，直接调 traceback.print_exc()
    会抛 AttributeError 引发二次崩溃，所以必须包一层。
    """
    try:
        traceback.print_exc()
    except Exception:
        pass


# ============================================================================
# 文本智能识别与处理（核心规则引擎）
# ============================================================================

def _is_readable_text(text):
    """
    判断一段 str 是否为“看起来正常的人类可读文本”。
    用于 Base64 解码结果的校验：解码出乱码/控制字符则视为不是文本。
    允许出现制表符和换行，其余必须是可打印字符。
    """
    if not text:
        return False
    for ch in text:
        if ch in '\t\n\r':
            continue
        if not ch.isprintable():
            return False
    return True


def process_text(raw):
    """
    按【优先级】依次尝试各条识别规则，全部失败则做纯文本清理兜底。

    返回值：(最终文本, 规则说明小字, 是否成功)
        最终文本     -> 写入剪贴板 / 显示到预览框的内容
        规则说明小字 -> 如 “✅ 检测为 JSON”
        是否成功     -> False 时预览框标红显示原文

    整个函数用 try...except 层层包裹，保证任何输入都不会抛异常导致崩溃。
    """
    try:
        # ---------------------------------------------------------------
        # 规则 1：JSON 检测
        # ---------------------------------------------------------------
        # 注意：只有以 { 或 [ 开头的文本才当作 JSON 尝试。
        # 否则像 "123"、"true" 这类纯标量也会被 json.loads 成功解析，
        # 导致后面的时间戳等规则永远轮不到执行。
        try:
            stripped = raw.strip()
            if stripped[:1] in ('{', '['):
                obj = json.loads(stripped)
                pretty = json.dumps(obj, ensure_ascii=False, indent=2)  # 缩进 2 空格，保留中文
                return pretty, '✅ 检测为 JSON', True
        except Exception:
            pass  # 解析失败 -> 继续尝试下一条规则，绝不直接放弃

        # ---------------------------------------------------------------
        # 规则 2：时间戳检测（10 位秒级 / 13 位毫秒级纯数字）
        # ---------------------------------------------------------------
        try:
            s = raw.strip()
            is_ms = bool(re.fullmatch(r'\d{13}', s))
            is_sec = bool(re.fullmatch(r'\d{10}', s))
            if is_sec or is_ms:
                ts = int(s) / 1000.0 if is_ms else int(s)
                dt = datetime.fromtimestamp(ts)  # 转本地时区时间
                # 合理范围检查：1970 ~ 2100 年；超出则视为普通数字，继续往下尝试
                if 1970 <= dt.year <= 2100:
                    label = '✅ 检测为 时间戳（毫秒）' if is_ms else '✅ 检测为 时间戳（秒）'
                    return dt.strftime('%Y-%m-%d %H:%M:%S'), label, True
        except Exception:
            pass  # 例如数值大到 datetime 无法表示，会抛 OverflowError/OSError，跳过即可

        # ---------------------------------------------------------------
        # 规则 3：URL 编码检测（包含 % 且解码后有变化才命中）
        # ---------------------------------------------------------------
        try:
            if '%' in raw:
                decoded = urllib.parse.unquote(raw)
                if decoded != raw and _is_readable_text(decoded):
                    return decoded, '✅ 检测为 URL 编码，已解码', True
                # decoded == raw 说明 % 都是普通字符（如 "50% off"），继续往下
        except Exception:
            pass

        # ---------------------------------------------------------------
        # 规则 4：Base64 检测（标准字母表 + 正确 padding + 结果为可读 UTF-8）
        #
        # 防误判（重要）：像 file / edit / byte / bits / also / city 这类普通英文
        # 单词，字符集与长度都“像” Base64，解码后恰好是可打印短串（"~)^"、"j[("）。
        # 旧版只要求“可打印”，会把单词静默替换成乱码写进剪贴板。
        # 现在额外要求解码结果 >= B64_MIN_TEXT_LEN 个字符 —— 该阈值是按语料量出来的，
        # 详见 B64_MIN_LEN 附近的说明；中文/长文本/JSON 等真 Base64 不受影响。
        # ---------------------------------------------------------------
        try:
            s = raw.strip()
            # 廉价外形筛查：长度达标、是 4 的倍数、字符集合法，
            # 避免把普通单词随便丢给 b64decode 造成无意义开销或误判
            if len(s) >= B64_MIN_LEN and re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', s):
                data = base64.b64decode(s, validate=True)   # validate=True 严格模式
                text = data.decode('utf-8')                  # 不是合法 UTF-8 会在这里抛异常
                if _is_readable_text(text) and len(text) >= B64_MIN_TEXT_LEN:
                    return text, '✅ 检测为 Base64，已解码', True
        except Exception:
            pass

        # ---------------------------------------------------------------
        # 规则 5：纯文本清理（兜底规则，几乎不会失败）
        # ---------------------------------------------------------------
        try:
            cleaned = raw.replace('\r\n', '\n').replace('\r', '\n')  # 统一换行符
            cleaned = cleaned.strip()                     # 去除首尾空白
            cleaned = re.sub(r'\n{2,}', '\n', cleaned)    # 连续换行压缩成单个换行
            # 连续空格/Tab 压缩成单个空格；换行符原样保留（[ \t] 不含 \n）
            cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
            return cleaned, '✅ 已执行 纯文本清理', True
        except Exception:
            pass

        # 理论上执行不到这里，防御性返回原文
        return raw, '', True

    except Exception:
        # 最外层兜底：任何未预料到的异常都显示原文并标红，绝不让程序崩溃
        tb()
        return raw, '⚠️ 处理失败，已显示原始文本', False


# ============================================================================
# 托盘图标绘制（程序内生成图片，不需要外部 .ico 文件）
# ============================================================================

def build_tray_image():
    """用 Pillow 画一个简易的“蓝色剪贴板”图标作为托盘图标。"""
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 剪贴板主体：白色圆角矩形 + 蓝色描边
    d.rounded_rectangle([10, 10, 54, 58], radius=8,
                        fill=(255, 255, 255, 255),
                        outline=(37, 99, 235, 255), width=4)
    # 顶部的夹子
    d.rounded_rectangle([22, 4, 42, 15], radius=4, fill=(37, 99, 235, 255))
    # 三条内容横线
    for y, x2 in ((26, 44), (36, 40), (46, 44)):
        d.line([20, y, x2, y], fill=(150, 170, 200, 255), width=4)
    return img


# ============================================================================
# 悬浮工具条窗口
# ============================================================================

class PasteMatePopup(tk.Toplevel):
    """
    无边框悬浮工具条。

    线程约定：所有方法只能在 tkinter 主线程中调用
    （其它线程通过 App.tasks 队列间接调度）。
    """

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self._copied = False          # 是否已经完成复制（进入 1.5s 倒计时阶段）
        self._result_text = ''        # 完整处理结果（预览可能截断，复制用这个）
        self._ever_focused = False    # 是否曾拿到过键盘焦点（失焦自毁判断依据）
        self._fg_attempts = 0         # 前台抢占重试计数

        self.overrideredirect(True)   # ★ 去掉标题栏和边框
        self.attributes('-topmost', True)  # 悬浮于所有窗口之上
        self.resizable(False, False)
        self.configure(bg=BG, highlightthickness=1,
                       highlightbackground=BORDER, highlightcolor=BORDER)
        self.withdraw()               # 先隐藏，等 show_near_cursor() 定位好再显示，避免闪位置

        # ---- 第一行控件几何位置：输入框 + 📋 按钮 ----
        btn_w = 70                                  # 按钮固定宽度（原 10% 槽宽约 70px）
        in_w = POPUP_W - PAD * 2 - GAP - btn_w      # 输入框吃掉剩余全部宽度
        in_x = PAD
        btn_x = in_x + in_w + GAP
        self._inner_w = POPUP_W - PAD * 2           # 预览/规则行的可用宽度

        # ---- 第一行左：输入框（粘进来的原文）----
        # ⚠️ 这里必须用 tk.Text，不能用 tk.Entry：
        # 实测 (2026-10-18) tk.Entry 处理超长字符串是 O(n²) 级别的，
        #   Entry.insert  8000 字符 = 1.2 秒, 16000 字符 = 4.9 秒,
        #   StringVar.set 8000 字符 = 1.2 秒, 16000 字符 = 4.7 秒；
        # 而同样操作 tk.Text 只要 0.0 ms。粘贴一大段 JSON 时 Entry 会把
        # tkinter 主线程卡住几十秒，Windows 判定“未响应/崩溃”——
        # 这就是“JSON 太长会崩溃”的真正原因（不是规则引擎，也不是预览面板）。
        self.input_box = tk.Text(
            self, font=FONT_UI, bg=FIELD_BG, fg=FG_MAIN, relief='flat',
            wrap='none', height=1, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
            insertbackground=FG_MAIN, padx=6, pady=3, undo=False,
            selectbackground=ACCENT, selectforeground='#FFFFFF',
        )
        self.input_box.place(x=in_x, y=CTRL_Y, width=in_w, height=CTRL_H)
        self._input_guard = False     # <<Modified>> 重入保护（见 _on_input_modified）

        # ---- 第一行右：复制按钮（图标 📋）----
        self.btn = tk.Button(
            self, text='📋', font=FONT_BTN,
            bg=ACCENT, fg='#FFFFFF', activebackground='#1D4ED8',
            activeforeground='#FFFFFF', relief='flat', bd=0,
            cursor='hand2', command=self.on_copy,
        )
        self.btn.place(x=btn_x, y=CTRL_Y, width=btn_w, height=CTRL_H)

        # ---- 第二行：全宽预览面板（只读 Text，多行 + 自动换行 + 可滚动）----
        # 不能用 tk.Entry：它是单行控件，换行符会渲染成零宽字符，
        # 于是 json.dumps(indent=2) 的结果在预览里被压成一行、后段完全看不到。
        # wrap 用 'none' 而不是 'word'：'word'/'char' 在超长单行上折行排版开销
        # 会突然暴涨（实测 4 千字符单行单次重绘就到 ~45ms，而 'none' 恒定 1~3ms，
        # 1MB 也只要 ~340ms）。预览本来就可以横向滚动，不必为好看去付这个代价。
        self.preview_box = tk.Text(
            self, font=FONT_UI, bg=FIELD_BG, fg=FG_PLACEHOLDER, relief='flat',
            wrap='none', highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=BORDER, padx=6, pady=3,
            insertwidth=0, takefocus=0, cursor='arrow',
        )
        self.preview_box.configure(state='disabled')   # 只读：用户不能改预览
        # 鼠标滚轮滚动预览（disabled 的 Text 不响应默认滚动）
        self.preview_box.bind('<MouseWheel>', self._on_preview_wheel)
        # Shift+滚轮 / 横向滚轮：左右滚动长行
        self.preview_box.bind('<Shift-MouseWheel>', self._on_preview_wheel_h)
        self.preview_box.bind('<Button-4>', lambda e: self._scroll_units(-3))
        self.preview_box.bind('<Button-5>', lambda e: self._scroll_units(3))
        self._preview_y = CTRL_Y + CTRL_H + GAP        # 预览面板的纵坐标（固定）

        # ---- 规则提示小字（预览面板下方，随面板高度浮动）----
        self.rule_lbl = tk.Label(
            self, text='', font=FONT_SMALL, bg=BG, fg=FG_PLACEHOLDER, anchor='w')
        # 高度给足余量：高 DPI 缩放下 emoji 字形比中文更高，太矮会裁掉底部
        self._rule_h = 21
        self._rule_lbl_pad = 2      # 预览面板与规则小字的间距

        # ---- “已复制”提示行（平时空白，复制成功时闪现）----
        self.toast_lbl = tk.Label(
            self, text='', font=('Microsoft YaHei UI', 10, 'bold'),
            bg=BG, fg=TOAST_FG, anchor='center')
        self._toast_h = 24

        # 先按“空内容”排一次版，让 _layout 算出初始窗口高度
        self._preview_h = PREVIEW_MIN_H
        self._layout()

        # ---- 事件绑定 ----
        # Text 没有 textvariable，用 <<Modified>> 监听内容变化（粘贴、键入、删除都覆盖）
        self.input_box.bind('<<Modified>>', self._on_input_modified)
        self.input_box.edit_modified(False)
        self.input_box.bind('<Return>', self._on_return)      # Enter = 复制（不插入换行）
        self.input_box.bind('<KP_Enter>', self._on_return)    # 小键盘 Enter 同理
        self.input_box.bind('<Escape>', lambda e: self.close())     # Esc = 关闭不复制
        self.bind('<Escape>', lambda e: self.close())
        # 失去焦点自动关闭：先延迟一小段时间再检查，
        # 给“点击自己内部按钮”这类焦点转移留出缓冲
        for w in (self.input_box, self.btn):
            w.bind('<FocusIn>', self._mark_focused, add='+')
            w.bind('<FocusOut>', self._schedule_focus_check, add='+')

        # 初始占位提示
        self._refresh_preview()

    # ------------------------------------------------------------------
    # 布局：预览面板高度自适应 + 整体定位
    # ------------------------------------------------------------------

    def _line_height(self):
        """当前字体的一行文字高度（像素）。取不到时按 30px（100% 缩放下的实测值）。"""
        try:
            import tkinter.font as tkfont
            return tkfont.Font(master=self, font=FONT_UI).metrics('linespace') or 30
        except Exception:
            return 30

    def _min_height(self):
        """空输入状态下的窗口高度（预览面板取最小高度）。"""
        return (CTRL_Y + CTRL_H + GAP          # 第一行
                + PREVIEW_MIN_H                # 预览面板
                + self._rule_lbl_pad + self._rule_h   # 规则小字
                + GAP + self._toast_h          # “已复制”提示行
                + PAD)                         # 底部留白

    def _preview_font(self):
        """预览面板实际使用的字体对象（缓存，measure 要用）。"""
        if getattr(self, '_pv_font', None) is None:
            try:
                import tkinter.font as tkfont
                self._pv_font = tkfont.Font(master=self, font=FONT_UI)
            except Exception:
                self._pv_font = None
        return self._pv_font

    def _measure_preview_lines(self):
        """
        估算预览内容在面板里会折成几行，用于决定面板高度。
        返回可视行数（已按 PREVIEW_MAX_LINES 截断）。

        ⚠️ 这里绝对不能用 Text.count(..., 'displaylines')：在长文本下它是
        超线性开销，实测单行 8 千字符就要 9.7 秒、1 万字符 16.5 秒 ——
        足以把 tkinter 主线程（也就是 GUI）彻底卡死，表现为“程序崩溃/未响应”。
        改为按字体度量自己估算：只测前 PROBE 个字符再按比例外推，
        并且只要凑够 PREVIEW_MAX_LINES 行就立刻返回，1MB 内容也只要几毫秒。
        """
        text = getattr(self, '_result_text', '') or ''
        if not text:
            return 1

        avail = max(1, self._inner_w - 16)      # 扣掉 Text 的 padx 与边框
        font = self._preview_font()
        if font is None:
            # 连字体都拿不到：退回按换行符数行（至少不会卡死）
            return max(1, min(text.count('\n') + 1, PREVIEW_MAX_LINES))

        PROBE = 400                 # 每行最多只测这么多字符，再按比例外推
        total = 0
        for logical in text.split('\n'):
            if not logical:
                total += 1
            else:
                probe = logical[:PROBE]
                try:
                    w = font.measure(probe)
                except Exception:
                    w = len(probe) * 8          # 兜底：按 8px/字符粗估
                if len(logical) > PROBE and w > 0:
                    w = int(w * len(logical) / len(probe))
                total += max(1, -(-w // avail))  # 等价于 ceil(w / avail)
            if total >= PREVIEW_MAX_LINES:      # 够高就不再往下算
                return PREVIEW_MAX_LINES
        return max(1, min(total, PREVIEW_MAX_LINES))

    def _layout(self, x=None, y=None):
        """
        依据预览内容行数重新排布：预览面板高度 → 规则小字 → “已复制”行 → 窗口高度。
        内容变长时窗口向下生长；已在屏幕上时会顺带做一次边缘避让。
        x / y 为 None 时沿用窗口当前位置。
        """
        try:
            lines = self._measure_preview_lines()
            self._preview_h = max(PREVIEW_MIN_H, lines * self._line_height() + 8)

            self.preview_box.place(x=PAD, y=self._preview_y,
                                   width=self._inner_w, height=self._preview_h)
            rule_y = self._preview_y + self._preview_h + self._rule_lbl_pad
            self.rule_lbl.place(x=PAD + 2, y=rule_y,
                                width=self._inner_w, height=self._rule_h)
            toast_y = rule_y + self._rule_h + GAP
            self.toast_lbl.place(x=0, y=toast_y, width=POPUP_W, height=self._toast_h)
            win_h = toast_y + self._toast_h + PAD

            if x is None or y is None:
                x, y = self.winfo_x(), self.winfo_y()
            # 变高后若超出屏幕底部，向上收一段（不会把窗口顶出屏幕顶部）
            vs = _virtual_screen()
            if vs:
                left, top, right, bottom = vs
                if y + win_h > bottom - 8:
                    y = max(top + 8, bottom - 8 - win_h)
            self.geometry('{}x{}+{}+{}'.format(POPUP_W, win_h, x, y))
        except Exception:
            tb()

    def _scroll_units(self, n):
        """竖直滚动预览 n 个单位（配合 Linux 风格的 Button-4/5 滚轮事件）。"""
        try:
            self.preview_box.yview_scroll(n, 'units')
        except Exception:
            pass
        return 'break'

    def _on_preview_wheel_h(self, event):
        """Shift+滚轮：横向滚动（预览是 wrap=none，长行需要左右看）。"""
        try:
            self.preview_box.xview_scroll(-1 if event.delta > 0 else 1, 'units')
        except Exception:
            pass
        return 'break'

    def _on_preview_wheel(self, event):
        """
        鼠标滚轮滚动预览面板。只读（disabled）的 Text 不响应默认滚动，
        这里手动滚动，并且仅当内容确实超出可视区域时才滚动，
        避免在内容很短时把画面滚歪。
        """
        try:
            first, last = self.preview_box.yview()
            if first <= 0.0 and last >= 1.0:
                return 'break'          # 内容没超出，不滚动
            self.preview_box.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        except Exception:
            pass
        return 'break'

    # ------------------------------------------------------------------
    # 显示 / 关闭
    # ------------------------------------------------------------------

    def show_near_cursor(self):
        """清空内容并把窗口定位到当前鼠标光标附近，抢夺键盘焦点。"""
        # 1) 取鼠标坐标（pynput 读坐标，简单可靠）
        mx, my = 300, 300
        try:
            from pynput.mouse import Controller as MouseController
            mx, my = MouseController().position
            mx, my = int(mx), int(my)
        except Exception:
            tb()

        # 2) 计算虚拟桌面边界（多显示器时含负坐标区域），防止窗口跑到屏幕外
        left, top, right, bottom = 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()
        vs = _virtual_screen()
        if vs:
            left, top, right, bottom = vs

        # 每次打开都从“空输入”状态开始，预览面板回到最小高度，
        # 所以这里按最小高度做边缘避让即可（后续长内容时窗口会向下长高）。
        win_h = self._min_height()

        x = mx + 14
        if x + POPUP_W > right - 8:
            x = right - 8 - POPUP_W
        if x < left + 8:
            x = left + 8
        y = my + 18
        if y + win_h > bottom - 8:
            y = my - win_h - 14   # 靠近屏幕底部时改为弹出在光标上方
        if y < top + 8:
            y = top + 8

        # 3) 重置状态并显示
        self._copied = False
        self._ever_focused = False
        try:
            self.btn.configure(state='normal')
        except Exception:
            pass
        self._set_input_text('')    # 每次打开都是全新一轮（会触发 _layout 复位高度）
        self._layout(x=x, y=y)      # 按当前内容重新排版并定位
        try:
            self.attributes('-topmost', True)   # 重新置顶，防止被其它置顶窗口压住
        except Exception:
            pass
        self.deiconify()
        self.lift()
        # Windows 有“前台锁定”机制：后台进程直接 focus_force 往往被系统静默拒绝，
        # 表现为窗口弹出来了却收不到键盘输入（看起来就像呼出失败）。
        # 必须走 “ALT 技巧 + SetForegroundWindow + 重试”，见 _acquire_focus_loop。
        self._fg_attempts = 0
        self.after(10, self._acquire_focus_loop)

    def reactivate(self):
        """已存在的窗口被热键再次唤起：重置内容并重新定位/聚焦。"""
        self.show_near_cursor()

    def close(self):
        """Esc 或点击外部时的关闭方式：不复制，直接销毁。"""
        self._safe_destroy()

    def _safe_destroy(self):
        try:
            if self.winfo_exists():
                self.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Windows 前台/焦点抢占（呼出可靠性的关键）
    #
    # Windows 的“前台锁定”机制会静默拒绝后台进程的 SetForegroundWindow /
    # focus_force 请求，表现为悬浮窗显示了但键盘焦点还在原应用里。
    # 经典解法（各类启动器/输入法工具条通用）：
    #   先模拟“按下并松开一次 Alt 键”，系统会临时放开前台锁，
    #   紧随其后的一次 SetForegroundWindow 就会被允许。
    # ------------------------------------------------------------------

    def _mark_focused(self, *_args):
        """FocusIn 回调：记录本窗口确实拿到过键盘焦点。"""
        self._ever_focused = True

    def _root_hwnd(self):
        """取本悬浮窗对应的 Win32 顶层句柄。"""
        try:
            import ctypes
            return ctypes.windll.user32.GetAncestor(self.winfo_id(), 2)  # GA_ROOT
        except Exception:
            return 0

    def _is_our_foreground(self):
        """当前系统前台窗口是否就是本悬浮窗。"""
        try:
            import ctypes
            return ctypes.windll.user32.GetForegroundWindow() == self._root_hwnd()
        except Exception:
            return False

    def _do_grab(self):
        """
        执行一次“抢前台 + 输入框聚焦”，三重手段叠加，成功率接近 100%：
          1) ALT 技巧：模拟按下并松开 Alt，部分系统版本会因此放开前台锁；
          2) AttachThreadInput 技巧：把我们的线程输入队列临时挂接到
             当前前台窗口所属线程，使系统允许我们调用 SetForegroundWindow
             （这是各类启动器工具最可靠的经典做法）；
          3) 全局钩子输入资格：本进程通过 pynput 低级钩子接收了用户按下的
             热键事件，Windows 视其为“最近接收输入的进程”，本身就允许抢前台。
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hwnd = self._root_hwnd()
            if not hwnd:
                return
            if user32.GetForegroundWindow() == hwnd:
                return   # 已经是前台了，无需再抢
            VK_MENU = 0x12           # ALT 虚拟键码
            KEYEVENTF_KEYUP = 0x2
            user32.keybd_event(VK_MENU, 0, 0, 0)                # [1] ALT 按下
            fg_hwnd = user32.GetForegroundWindow()
            cur_tid = kernel32.GetCurrentThreadId()
            fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
            attached = False
            if fg_hwnd and fg_tid and fg_tid != cur_tid:
                # [2] 临时挂接输入队列（失败也不影响后续尝试）
                attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, 1))
            try:
                ok = bool(user32.SetForegroundWindow(hwnd))
                # 顺便强制置顶显示（HWND_TOPMOST + NOMOVE|NOSIZE|SHOWWINDOW）
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0043)
            finally:
                if attached:
                    user32.AttachThreadInput(cur_tid, fg_tid, 0)   # 解除挂接
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)  # ALT 松开
            if DEBUG:
                log('grab: SetForegroundWindow ->', ok)
        except Exception:
            tb()
        try:
            self.input_box.focus_force()   # 前台到手后把 tk 焦点钉到输入框
        except Exception:
            pass

    def _acquire_focus_loop(self):
        """带重试的前台抢占入口：由 show_near_cursor 定时触发。"""
        if self._copied or not self.winfo_exists():
            return
        self._do_grab()
        self.after(70, self._verify_focus)

    def _verify_focus(self):
        """检查抢占结果；未成功则继续重试，最多约 10 次（~1 秒）。

        即使全部失败也不关闭窗口：用户手动点击输入框仍可正常获得焦点，
        功能只是退化为“先点一下再粘贴”。
        """
        if self._copied or not self.winfo_exists():
            return
        if self._is_our_foreground():
            try:
                self.input_box.focus_force()   # 前台已到手，此时必然生效
            except Exception:
                pass
            if DEBUG:
                log('focus acquired after', self._fg_attempts, 'retries')
            return
        self._fg_attempts += 1
        if DEBUG:
            log('focus retry #', self._fg_attempts)
        if self._fg_attempts <= 10:
            self.after(90, self._acquire_focus_loop)

    # ------------------------------------------------------------------
    # 失去焦点自动关闭
    # ------------------------------------------------------------------

    def _schedule_focus_check(self, *_args):
        """FocusOut 回调：稍等片刻再做判断，避免误杀自己内部的点击。"""
        if self._copied:
            return  # 已在关闭倒计时内，不用管
        try:
            self.after(140, self._focus_check_close)
        except Exception:
            pass

    def _focus_check_close(self):
        """真正判断：当前焦点若不在本窗口内的任何控件上，就自动关闭。"""
        if self._copied or not self.winfo_exists():
            return
        if not self._ever_focused:
            # 从未拿到过键盘焦点（被 Windows 前台锁拒绝了）：
            # 这不算“用户点击了别处”，交给 _acquire_focus_loop 继续重试
            return
        try:
            focused = self.focus_get()
        except Exception:
            focused = None
        if focused is None:
            self.close()
            return
        try:
            if focused.winfo_toplevel() is not self:
                self.close()
        except Exception:
            self.close()

    # ------------------------------------------------------------------
    # 预览实时刷新
    # ------------------------------------------------------------------

    # ---- 输入框（Text）的读写与事件 ----

    def _get_input_text(self):
        """取输入框全文（Text 会自带一个末尾换行，要去掉）。"""
        try:
            return self.input_box.get('1.0', 'end-1c')
        except Exception:
            return ''

    def _set_input_text(self, text, refresh=True):
        """
        替换输入框内容，并把“已修改”标记复位（避免递归触发）。

        注意：guard 只是挡住 <<Modified>> 回调，不能让预览“停在上一次的结果”——
        那会导致重新打开弹窗时仍显示上一轮的内容。所以这里在 guard 释放后
        主动刷新一次预览（refresh=False 可显式跳过）。
        """
        self._input_guard = True
        try:
            self.input_box.delete('1.0', 'end')
            if text:
                self.input_box.insert('1.0', text)
            self.input_box.edit_modified(False)
        except Exception:
            tb()
        finally:
            self._input_guard = False
        if refresh:
            self._refresh_preview()

    def _on_input_modified(self, event=None):
        """
        Text 内容变化回调。<<Modified>> 会在每次编辑后触发，
        必须先复位 edit_modified 标记，否则不会再次触发。
        用 _input_guard 挡住 _set_input_text 自身引起的回调。
        """
        if self._input_guard:
            return
        try:
            if not self.input_box.edit_modified():
                return
            self.input_box.edit_modified(False)
        except Exception:
            pass
        self._refresh_preview()

    def _on_return(self, event=None):
        """输入框里按 Enter：复制（而不是往 Text 里插入换行）。"""
        self.on_copy()
        return 'break'          # 阻止默认的换行插入行为

    def _set_preview_text(self, text):
        """
        把文本写进只读预览面板（写前临时解禁，写完立即恢复只读）。

        超长内容只渲染前 PREVIEW_MAX_CHARS 个字符：预览面板最多显示 8 行，
        再多也看不见，但把整份内容塞进 Text 会让控件自身变慢（几十万行时
        光插入与重排就够呛）。完整结果始终留在 self._result_text 里，
        复制时用的是它 —— 预览截断不等于输出截断。

        返回 True 表示预览被截断（调用方据此给出提示）。
        """
        truncated = False
        if text and len(text) > PREVIEW_MAX_CHARS:
            text = text[:PREVIEW_MAX_CHARS]
            truncated = True
        box = self.preview_box
        box.configure(state='normal')
        box.delete('1.0', 'end')
        if text:
            box.insert('1.0', text)
        box.configure(state='disabled')
        box.yview_moveto(0.0)          # 每次刷新都回到顶部，避免残留滚动位置
        return truncated

    def _refresh_preview(self, *_args):
        """输入框内容变化时调用：跑一遍识别规则，更新预览面板与规则小字。"""
        raw = self._get_input_text()
        if raw == '':
            self._result_text = ''
            self._set_preview_text('')
            self.preview_box.configure(fg=FG_PLACEHOLDER)
            self.rule_lbl.configure(
                text='⌨️ 输入 / 粘贴内容后自动识别 · Enter=复制 · Esc=关闭',
                fg=FG_PLACEHOLDER)
            if not self._copied:
                # 空输入时禁用复制按钮（配合 on_copy 的空串保护），有内容后再启用
                try:
                    self.btn.configure(state='disabled')
                except Exception:
                    pass
            self._layout()          # 收回到最小高度
            return
        try:
            result, label, ok = process_text(raw)
        except Exception:
            # process_text 内部已兜底，这里属于双保险
            result, label, ok = raw, '⚠️ 处理失败，已显示原始文本', False
        # 完整结果留在这里：预览可能截断，复制绝不能用截断后的内容
        self._result_text = result
        truncated = self._set_preview_text(result)
        self.preview_box.configure(fg=(FAIL_FG if not ok else FG_MAIN))
        if truncated:
            # 预览有截断时说清楚：免得用户以为“复制也只能拿到这些”
            label = '{}（预览仅显示前 {:,} 字符，复制仍为完整内容）'.format(
                label, PREVIEW_MAX_CHARS)
        self.rule_lbl.configure(text=label, fg=(RULE_FAIL_FG if not ok else RULE_OK_FG))
        if not self._copied:
            try:
                self.btn.configure(state='normal')
            except Exception:
                pass
        self._layout()              # 预览变长/变短时同步调整面板与窗口高度

    # ------------------------------------------------------------------
    # 复制动作
    # ------------------------------------------------------------------

    def on_copy(self):
        """把预览框最终文本写入剪贴板 → 闪现提示 → 1.5 秒后自动关闭。"""
        if not self.winfo_exists() or self._copied:
            return
        # 用完整处理结果（预览面板可能只渲染了前 PREVIEW_MAX_CHARS 个字符，
        # 从 Text 里取会把超长内容截断后再写进剪贴板）
        final_text = getattr(self, '_result_text', '')
        if not final_text:
            # 兜底：_result_text 意外为空时退回读预览面板
            final_text = self.preview_box.get('1.0', 'end-1c')
        if not final_text:
            # BUG-001：空输入时不执行复制，避免把剪贴板覆盖成空字符串；
            # 给出可见反馈（一旦开始输入，这行小字会被刷新逻辑自动覆盖）
            self.rule_lbl.configure(text='⚠️ 内容为空，未复制', fg=RULE_FAIL_FG)
            return
        try:
            try:
                pyperclip.copy(final_text)
            except Exception:
                # 兜底方案：tkinter 自带剪贴板操作（极少用到）
                self.clipboard_clear()
                self.clipboard_append(final_text)
                self.update_idletasks()
            # 进入“已复制”阶段：锁定按钮、闪现提示、定时自杀
            self._copied = True
            self.btn.configure(state='disabled')
            self.toast_lbl.configure(text='✅ 已复制到剪贴板', fg=TOAST_FG)
            self.rule_lbl.configure(text='✅ 已写入剪贴板，即将自动关闭…', fg=RULE_OK_FG)
            self.after(TOAST_MS, self._safe_destroy)
        except Exception:
            tb()
            self._safe_destroy()


def _virtual_screen():
    """
    返回虚拟桌面边界 (left, top, right, bottom)，多显示器时包含负坐标区域。
    获取失败返回 None，由调用方回退到主屏尺寸。
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        l, t = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        w, h = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        if w > 0 and h > 0:
            return l, t, l + w, t + h
    except Exception:
        pass
    return None


# ============================================================================
# Win32 原生全局热键（RegisterHotKey 备用通道）
# ============================================================================

# 修饰键 -> Win32 MOD_* 标志
_WIN_MOD_MAP = {
    '<ctrl>': 0x0002,   # MOD_CONTROL
    '<shift>': 0x0004,  # MOD_SHIFT
    '<alt>': 0x0001,    # MOD_ALT
    '<win>': 0x0008,    # MOD_WIN
}


def parse_win_hotkey(spec):
    """
    把 pynput 风格热键字符串（如 '<ctrl>+<shift>+f'）解析成
    Win32 RegisterHotKey 需要的 (修饰键标志, 虚拟键码)。
    解析失败返回 None。
    """
    try:
        mods = 0
        vk = None
        for part in spec.split('+'):
            p = part.strip()
            pl = p.lower()
            if pl in _WIN_MOD_MAP:
                mods |= _WIN_MOD_MAP[pl]
            elif len(p) == 1:
                vk = ord(p.upper())          # 普通字符键
            else:
                # 具名键（如 <f5>、<home>）：借助 pynput 的键表查虚拟键码。
                # 注意 pynput 的 Key 枚举名不带尖括号（Key.f5 / Key.home），
                # 所以必须先把 < > 去掉再查表，否则必然 KeyError，
                # 表现为“自定义成具名键后 Win32 备用通道被静默禁用”。
                try:
                    key = keyboard.Key[pl.strip('<>')]
                    vk = key.value.vk
                except KeyError:
                    # ISSUE-003：具名键写错时明确提示，而不是静默失败
                    log('热键解析失败：未知按键名 "{}"（来自 "{}"，'
                        '该热键将不注册 Win32 备用通道）'.format(p, spec))
                    return None
        if vk is None:
            return None
        return mods, vk
    except Exception:
        tb()
        return None


# ============================================================================
# 单实例互斥 + 唤醒通道
# ============================================================================

class SingletonConflict(Exception):
    """单实例端口被“非 PasteMate”的其它程序占用时抛出。"""


def acquire_singleton(port=SINGLETON_PORT):
    """
    用“绑定本机回环 UDP 端口”实现互斥锁（同一端口只能 bind 一次），
    同时兼作唤醒通道（老进程在此端口收消息）。

    端口策略（重要）：
      依次尝试候选端口 [port, port+1, port+2]，按 bind 失败的原因分流：
        * WinError 10048（端口被进程占用）-> 与占用者握手：
            - 应答 PMATE!（新版 PasteMate）-> 发送 SHOW 唤醒后返回 None；
            - 无应答（旧版 PasteMate 或陌生程序）-> 补发一次旧协议 SHOW
              尝试唤醒旧版，然后抛 SingletonConflict 提示用户；
        * 其它错误（如 WinError 10013 拒绝访问）-> 多半是端口落入了
          Windows/Hyper-V 的“排除端口范围”（netsh 可查，会动态变化，
          没有任何进程持有它），直接静默换下一个候选端口。

    返回值：
      * socket 对象 -> 本进程是第一个实例，负责监听唤醒消息；
      * None        -> 已有 PasteMate 实例在运行，本进程应立即退出。
    抛出：
      * SingletonConflict -> 所有候选端口都不可用时。
    """
    # 候选端口：主端口 + 7 个大间隔备用端口。
    # 间隔拉开到总跨度 600+，确保任何连续的系统保留段（通常 100 端口宽，
    # 最多相邻几段）都不可能把所有候选一网打尽。
    offsets = (0, 37, 89, 151, 233, 331, 451, 601)
    candidates = [port + off for off in offsets]
    for i, p in enumerate(candidates):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(('127.0.0.1', p))
            sock.setblocking(False)   # 非阻塞，主循环里轮询收包
            if i > 0:
                log('端口 {} 不可用（被系统保留或占用），已自动改用备用端口 {}'
                    .format(candidates[0], p))
            return sock
        except OSError as e:
            sock.close()
            winerr = getattr(e, 'winerror', None) or e.errno
            if winerr != 10048:
                # 10013 等：端口被系统保留（排除范围），没有进程持有它，
                # 换下一个候选端口即可（握手没有意义，对方不存在）
                log('端口 {} bind 失败（winerror={}，疑似系统保留端口），'
                    '尝试下一个候选端口'.format(p, winerr))
                continue
            # ---- 10048：端口确实被某个进程占用，走握手确认流程 ----
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                probe.settimeout(0.8)
                probe.connect(('127.0.0.1', p))
                probe.send(b'PMATE?')
                try:
                    reply = probe.recv(16)
                except Exception:
                    reply = b''      # 无应答 => 占用者不是（新版）PasteMate
                if reply.startswith(b'PMATE!'):
                    # 确认是 PasteMate 实例：发送唤醒指令后正常退出
                    probe.send(b'SHOW')
                    probe.close()
                    return None
                # 握手无应答：可能是【旧版本 PasteMate】（不认识握手协议），
                # 补发一次旧协议的 SHOW 尝试唤醒；若是陌生程序则该包会被忽略
                try:
                    probe.send(b'SHOW')
                except Exception:
                    pass
                probe.close()
            except Exception:
                tb()
            # 端口被占用且无法确认身份：让用户知道原因，而不是静默失败
            raise SingletonConflict(
                '单实例端口 {} 被其它程序占用，新版 PasteMate 无法启动。\n\n'
                '可能原因与解决方法：\n'
                '  1) 若托盘里还运行着【旧版】PasteMate，请先右键托盘图标退出'
                '旧版，再启动新版；\n'
                '  2) 若是其它程序占用了端口，请设置环境变量换端口后重试，'
                '例如：set PASTEMATE_PORT=53999'.format(p))

    # 所有候选端口都因系统保留等原因不可用
    raise SingletonConflict(
        '候选端口 {} 均被系统保留，无法启动。\n\n'
        '请设置环境变量 PASTEMATE_PORT 指定一个空闲端口后重试，'
        '例如：set PASTEMATE_PORT=53999'.format(candidates))


# ============================================================================
# 开机自启（HKCU Run 注册表项，普通用户权限即可，无需管理员）
# ============================================================================

_AUTOSTART_REG_PATH = r'Software\Microsoft\Windows\CurrentVersion\Run'
_AUTOSTART_VALUE_NAME = APP_NAME   # 注册表值名，任务管理器“启动”页会显示它


def _autostart_command():
    """
    生成写进注册表的启动命令行：
      "pythonw.exe绝对路径" "pastemate.py绝对路径"
    若当前运行的是带控制台的 python.exe，自动换成同目录的 pythonw.exe，
    避免每次开机弹出一个黑色控制台窗口。
    """
    exe = sys.executable
    if exe.lower().endswith('\\python.exe'):
        pythonw = exe[:-len('python.exe')] + 'pythonw.exe'
        if os.path.exists(pythonw):
            exe = pythonw
    script = os.path.abspath(__file__)
    return '"{}" "{}"'.format(exe, script)


def autostart_enabled():
    """查询当前是否已开启开机自启。查询失败一律视为未开启。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH,
                            0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, _AUTOSTART_VALUE_NAME)
        return True
    except OSError:
        return False


def set_autostart(enable):
    """开启/关闭开机自启。成功返回 True，失败返回 False（不抛异常）。"""
    if winreg is None:
        log('当前环境无 winreg 模块，开机自启功能不可用')
        return False
    try:
        if enable:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH,
                                0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, _AUTOSTART_VALUE_NAME, 0,
                                  winreg.REG_SZ, _autostart_command())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH,
                                    0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, _AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                pass   # 本来就没开，视为成功
        return True
    except Exception:
        tb()
        return False


# ============================================================================
# 应用主体：托盘图标 + 全局热键 + 主循环调度
# ============================================================================

class PasteMateApp:
    """
    线程模型（重要）：
      * 主线程：tkinter mainloop + 定时轮询（POLL_MS）；
      * 托盘线程（daemon）：pystray icon.run()；
      * 热键监听线程（daemon）：pynput GlobalHotKeys。
    托盘/热键回调都发生在各自线程里，一律通过 tasks 队列把真正的 UI
    操作投递回主线程执行，避免多线程操作 tkinter 造成崩溃。
    """

    def __init__(self, singleton_sock):
        self.singleton_sock = singleton_sock
        self.popup = None              # 当前悬浮窗引用（可能为 None）
        self.tasks = queue.Queue()     # 跨线程任务队列：存放“要在主线程执行的函数”
        self._last_show = 0.0          # 上次弹窗时间戳（双通道去抖用）
        self._winhk_tid = 0            # Win32 热键工作线程 ID（退出时投递 WM_QUIT）

        # --- tkinter 主窗口：只作容器，永不显示 ---
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)

        # --- 托盘图标 ---
        menu = pystray.Menu(
            pystray.MenuItem(
                '显示工具条 (Ctrl+Shift+F)',
                lambda icon, item: self.tasks.put(self.show_popup),
                default=True),   # default=True：双击托盘图标也触发
            pystray.MenuItem(
                '开机自启',
                self._toggle_autostart,
                # checked 回调决定菜单项前是否显示勾选标记
                checked=lambda item: autostart_enabled()),
            pystray.MenuItem(
                '退出',
                lambda icon, item: self.request_exit()),
        )
        self.icon = pystray.Icon(APP_NAME, build_tray_image(),
                                 '{} v{} — 按Ctrl+Shift+F呼出'.format(APP_NAME, APP_VERSION),
                                 menu)
        threading.Thread(target=self.icon.run, daemon=True, name='tray').start()

        # --- 全局热键（pynput 主通道 + Win32 原生备用通道）---
        self._setup_hotkey()
        self._start_winhook()

        # --- 开机自启自愈：若用户已开启自启，刷新注册表中的路径，
        #     防止程序被移动/重命名后开机启动失效 ---
        self._refresh_autostart()

        # --- 开始主循环轮询 ---
        self.root.after(POLL_MS, self._poll)

        # --- tkinter 回调异常兜底 ---
        # pythonw 下 sys.stderr 是 None，tkinter 默认的回调异常打印会
        # 自己再抛 AttributeError，直接杀死主循环导致进程静默退出。
        # 这里接管过来，保证任何回调异常都只记日志、绝不致命。
        self.root.report_callback_exception = self._on_tk_exception

    @staticmethod
    def _on_tk_exception(etype, evalue, etb):
        log('tkinter 回调异常:')
        try:
            import traceback as _tr
            _tr.print_exception(etype, evalue, etb)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 开机自启（托盘菜单开关）
    # ------------------------------------------------------------------

    def _toggle_autostart(self, icon=None, item=None):
        """
        切换开机自启状态。

        注意：必须在托盘线程【同步】完成切换，不能丢进任务队列延后执行！
        原因：pystray 在菜单项动作函数返回后会立即重建菜单并重新求值
        checked 回调——如果此时注册表还没改完，菜单就会显示切换前的
        状态，表现为勾选状态永远“反着来”。
        winreg 读写很快且与 tkinter 无关，在托盘线程直接执行没有风险；
        只有涉及 tkinter 的操作（root.after）才需要走任务队列回主线程。
        """
        enable = not autostart_enabled()
        if set_autostart(enable):
            log('开机自启已{}'.format('开启' if enable else '关闭'))
            try:
                self.icon.notify('已{}开机自启'.format('开启' if enable else '关闭'),
                                 APP_NAME)
            except Exception:
                pass   # 气泡通知失败无伤大雅
        else:
            try:
                self.icon.notify('设置开机自启失败（权限或注册表异常）', APP_NAME)
            except Exception:
                pass
        try:
            # 立即按新状态重建托盘菜单，保证勾选标记与实际状态一致
            self.icon.update_menu()
        except Exception:
            pass
        # 气泡 2.5 秒后自动消失（root.after 只能在主线程调用，走任务队列）
        self.tasks.put(self._schedule_notify_removal)

    def _schedule_notify_removal(self):
        """主线程执行：定时移除托盘气泡。"""
        self.root.after(2500, self._remove_notify)

    def _remove_notify(self):
        """移除托盘气泡通知。"""
        try:
            self.icon.remove_notification()
        except Exception:
            pass

    def _refresh_autostart(self):
        """已开启自启时，把注册表里的命令行刷新为当前实际路径。"""
        if autostart_enabled():
            set_autostart(True)

    # ------------------------------------------------------------------

    def _setup_hotkey(self):
        """注册全局热键（pynput 主通道）。回调在监听线程里执行，只入队不动 UI。"""
        try:
            self.hotkey_listener = keyboard.GlobalHotKeys(
                {HOTKEY: lambda: self.tasks.put(self.show_popup)})
            self.hotkey_listener.start()
            log('全局热键已注册(pynput):', HOTKEY)
        except Exception:
            # 极少数环境钩子注册失败：仍有 Win32 备用通道和托盘菜单可用
            self.hotkey_listener = None
            tb()

    def _start_winhook(self):
        """
        注册 Win32 原生全局热键（RegisterHotKey 备用通道）。

        作用：
          * 不依赖低级键盘钩子——即使安全软件拦截钩子、或 pynput 异常，
            热键依然有效；
          * 对“注入事件”也响应，便于自动化测试。
        代价：
          * 该组合键会被系统占用，PasteMate 运行期间其它程序收不到它。
        实现：
          RegisterHotKey 必须在拥有消息循环的线程上使用，所以这里开一个
          专用 daemon 线程跑 GetMessageW 循环，收到 WM_HOTKEY 后把弹窗
          任务投递回主线程。
        """
        parsed = parse_win_hotkey(HOTKEY)
        if parsed is None:
            log('热键字符串无法用于 Win32 注册，跳过备用通道:', HOTKEY)
            return
        mods, vk = parsed

        def worker():
            try:
                import ctypes
                import ctypes.wintypes
                user32 = ctypes.windll.user32
                HK_ID = 0xB00B   # 任意非零 ID
                if not user32.RegisterHotKey(None, HK_ID, mods, vk):
                    log('RegisterHotKey 注册失败(组合键可能被其它程序占用)，'
                        '将仅使用 pynput 钩子通道')
                    return
                # 记录线程 ID，供退出时投递 WM_QUIT 结束消息循环
                self._winhk_tid = ctypes.windll.kernel32.GetCurrentThreadId()
                log('全局热键已注册(Win32 备用通道)')
                msg = ctypes.wintypes.MSG()
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == 0x0312:          # WM_HOTKEY
                        self.tasks.put(self.show_popup)
                user32.UnregisterHotKey(None, HK_ID)
            except Exception:
                tb()

        threading.Thread(target=worker, daemon=True, name='win-hotkey').start()

    # ------------------------------------------------------------------
    # 悬浮窗管理
    # ------------------------------------------------------------------

    def show_popup(self):
        """显示工具条（已在运行则重新定位+聚焦）。主线程执行。"""
        # 双通道（pynput + Win32）可能几乎同时触发，0.3 秒内只算一次
        now = time.time()
        if now - self._last_show < 0.3:
            return
        self._last_show = now
        try:
            if self.popup is not None and self.popup.winfo_exists():
                self.popup.reactivate()
            else:
                self.popup = PasteMatePopup(self)
                self.popup.show_near_cursor()
        except Exception:
            tb()

    def request_exit(self):
        """线程安全的退出请求：投递到主线程执行真正的清理。"""
        self.tasks.put(self._shutdown)

    def _shutdown(self):
        log('正在退出…')
        try:
            # 先结束 Win32 热键线程的消息循环（PostThreadMessage 投递 WM_QUIT）
            if self._winhk_tid:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(self._winhk_tid, 0x0012, 0, 0)
        except Exception:
            pass
        try:
            if getattr(self, 'hotkey_listener', None) is not None:
                self.hotkey_listener.stop()
        except Exception:
            pass
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass
        try:
            if self.singleton_sock is not None:
                self.singleton_sock.close()
        except Exception:
            pass
        try:
            self.root.quit()   # 结束 mainloop，main() 收尾后进程退出
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 主循环轮询：单实例消息 + 跨线程任务队列
    # ------------------------------------------------------------------

    def _poll(self):
        try:
            self._drain_singleton()
            self._drain_tasks()
        except Exception:
            tb()
        finally:
            # 无论发生什么都继续排定下一轮轮询，保证主循环不死
            self.root.after(POLL_MS, self._poll)

    def _drain_singleton(self):
        """非阻塞收取其它实例发来的唤醒消息。"""
        sock = self.singleton_sock
        if sock is None:
            return
        while True:
            try:
                data, addr = sock.recvfrom(64)
            except (BlockingIOError, InterruptedError):
                break                    # 暂无数据，正常返回
            except OSError:
                self.singleton_sock = None
                break                    # socket 已失效，放弃监听但不影响主功能
            if data.strip().startswith(b'PMATE?'):
                # 握手应答：告诉对方“我是 PasteMate 实例”
                try:
                    sock.sendto(b'PMATE!', addr)
                except Exception:
                    tb()
            elif data.strip().startswith(b'SHOW'):
                self.show_popup()

    def _drain_tasks(self):
        """执行其它线程投递过来的函数（如 show_popup / _shutdown）。"""
        while True:
            try:
                fn = self.tasks.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                tb()

    # ------------------------------------------------------------------

    def run(self):
        self.root.mainloop()


# ============================================================================
# 入口
# ============================================================================

def enable_dpi_awareness():
    """
    开启 DPI 感知：高分屏下字体清晰、GetCursorPos 与窗口坐标系一致。
    失败完全不影响运行（仅显示效果略差），所以全部吞异常。
    """
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()         # 老系统回退
    except Exception:
        pass


def main():
    enable_dpi_awareness()
    log('{} v{} starting...'.format(APP_NAME, APP_VERSION))

    # ---- 单实例检查：已是第二个进程则唤醒老进程并退出 ----
    try:
        sock = acquire_singleton()
    except SingletonConflict as e:
        # 端口被陌生程序占用：日志 + 弹窗明确告知（冒烟测试模式下只写日志）
        log(str(e))
        if os.environ.get('PASTEMATE_SMOKE') != '1':
            try:
                import ctypes
                # 0x30=MB_ICONWARNING, 0x1000=MB_SYSTEMMODAL（置顶显示，不被遮挡）
                ctypes.windll.user32.MessageBoxW(
                    None, str(e), 'PasteMate 启动失败', 0x30 | 0x1000)
            except Exception:
                pass
        sys.exit(2)
    if sock is None:
        log('检测到已有 PasteMate 实例，已唤起其工具条，本进程退出。')
        return

    app = PasteMateApp(sock)

    # 自动化冒烟测试钩子：设置环境变量 PASTEMATE_SMOKE=1 时启动 2.5 秒后自动退出
    if os.environ.get('PASTEMATE_SMOKE') == '1':
        app.root.after(2500, app.request_exit)

    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if app.singleton_sock is not None:
                app.singleton_sock.close()
        except Exception:
            pass
    log('已退出。')


if __name__ == '__main__':
    main()
