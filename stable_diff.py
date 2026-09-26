"""HtmlDiff 递归失败时的完整、非递归左右差异表。

行级 SequenceMatcher 用显式工作队列实现匹配；替换块按顺序配对，
每对独立计算行内差异。只改变相似行的对齐方式，不省略变更或修改统计。
"""

import difflib
import html
import io
from itertools import zip_longest
from uuid import uuid4


def _escape(text):
    return html.escape(text, quote=False).replace(" ", "&nbsp;").replace("\t", "&nbsp;")


def _highlight(text, kind):
    return f'<span class="diff_{kind}">{_escape(text) or "&nbsp;"}</span>'


def _changed_pair(old, new):
    left, right = [], []
    matcher = difflib.SequenceMatcher(difflib.IS_CHARACTER_JUNK, old, new, autojunk=False)
    for tag, i, j, k, l in matcher.get_opcodes():
        if tag == "equal":
            left.append(_escape(old[i:j]))
            right.append(_escape(new[k:l]))
        else:
            if i != j:
                left.append(_highlight(old[i:j], "chg" if tag == "replace" else "sub"))
            if k != l:
                right.append(_highlight(new[k:l], "chg" if tag == "replace" else "add"))
    return "".join(left), "".join(right)


def _line_groups(old, new, context, numlines):
    # Do not feed a long, unchanged repeated prefix/suffix to the exact matcher:
    # autojunk=False otherwise revisits every pair of those identical lines.
    start = 0
    limit = min(len(old), len(new))
    while start < limit and old[start] == new[start]:
        start += 1
    old_end, new_end = len(old), len(new)
    while old_end > start and new_end > start and old[old_end - 1] == new[new_end - 1]:
        old_end -= 1
        new_end -= 1

    if context:
        # Retain the requested boundary context before grouping internal edits.
        offset = max(0, start - numlines)
        matcher = difflib.SequenceMatcher(
            None, old[offset:min(len(old), old_end + numlines)],
            new[offset:min(len(new), new_end + numlines)], autojunk=False,
        )
        for group in matcher.get_grouped_opcodes(numlines):
            yield [(tag, i + offset, j + offset, k + offset, l + offset)
                   for tag, i, j, k, l in group]
    else:
        matcher = difflib.SequenceMatcher(
            None, old[start:old_end], new[start:new_end], autojunk=False,
        )
        codes = []
        if start:
            codes.append(("equal", 0, start, 0, start))
        codes.extend((tag, i + start, j + start, k + start, l + start)
                     for tag, i, j, k, l in matcher.get_opcodes())
        if old_end < len(old):
            codes.append(("equal", old_end, len(old), new_end, len(new)))
        yield codes


def make_table(old_lines, new_lines, fromdesc="", todesc="", context=False, numlines=3):
    # 复用标准库的 tab 表示，避免把原文 tab 与等宽空格误认为相同内容。
    old, new = difflib.HtmlDiff(tabsize=4)._tab_newline_replace(old_lines, new_lines)
    groups = _line_groups(old, new, context, numlines)
    prefix = "stable_" + uuid4().hex
    out = io.StringIO()
    out.write('<div class="diff-render-note">使用完整逐行对比：替换段按行序对齐，保留行内差异。'
              + ('当前按所选设置显示差异上下文。' if context else '已显示全部内容。') + '</div>')
    out.write(f'<table class="diff" id="{prefix}"><thead><tr>'
              '<th class="diff_next"></th><th colspan="2" class="diff_header">'
              + fromdesc + '</th><th class="diff_next"></th>'
              '<th colspan="2" class="diff_header">' + todesc + '</th></tr></thead>')

    def cell(number, content, side):
        if number is None:
            return '<td class="diff_header"></td><td nowrap="nowrap"></td>'
        return (f'<td class="diff_header" id="{prefix}_{side}{number + 1}">{number + 1}</td>'
                f'<td nowrap="nowrap">{content}</td>')

    wrote_rows = False
    for group in groups:
        out.write('<tbody>')
        for tag, i, j, k, l in group:
            for a, b in zip_longest(range(i, j), range(k, l)):
                if tag == "equal":
                    left, right = _escape(old[a]), _escape(new[b])
                elif a is None:
                    left, right = "", _highlight(new[b], "add")
                elif b is None:
                    left, right = _highlight(old[a], "sub"), ""
                else:
                    left, right = _changed_pair(old[a], new[b])
                out.write('<tr><td class="diff_next"></td>' + cell(a, left, "old")
                          + '<td class="diff_next"></td>' + cell(b, right, "new") + '</tr>\n')
                wrote_rows = True
        out.write('</tbody>')
    if not wrote_rows:
        out.write('<tbody><tr><td colspan="6">'
                  + ('没有内容差异' if context else '空文件') + '</td></tr></tbody>')
    out.write('</table>')
    return out.getvalue()


def prefer_stable_diff(old_lines, new_lines):
    """提前选择完整逐行渲染，不等待 HtmlDiff 的相似行递归失败。

    64 仅是算法切换点，不是输入/输出上限；两种路径均保留全部明细。
    小修改继续使用原来的相似行对齐，避免无关的视觉变化。
    """
    minimum = 64
    if min(len(old_lines), len(new_lines)) < minimum:
        return False
    start = 0
    limit = min(len(old_lines), len(new_lines))
    while start < limit and old_lines[start] == new_lines[start]:
        start += 1
    old_end, new_end = len(old_lines), len(new_lines)
    while old_end > start and new_end > start and old_lines[old_end - 1] == new_lines[new_end - 1]:
        old_end -= 1
        new_end -= 1
    if min(old_end - start, new_end - start) < minimum:
        return False
    # 与 HtmlDiff 的行级匹配采用相同启发式，只判定是否存在大替换段。
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    return any(
        tag == "replace" and min(j - i, l - k) >= minimum
        for tag, i, j, k, l in matcher.get_opcodes()
    )
