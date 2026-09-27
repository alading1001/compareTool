"""HtmlDiff 递归失败时的完整、非递归左右差异表。

行级 SequenceMatcher 用显式工作队列实现匹配；替换块按顺序配对，
每对独立计算行内差异。只改变相似行的对齐方式，不省略变更或修改统计。
"""

import difflib
import html
from itertools import zip_longest
from uuid import uuid4


def _escape(text):
    return html.escape(text, quote=False).replace(" ", "&nbsp;").replace("\t", "&nbsp;")


def _highlight(text, kind):
    return f'<span class="diff_{kind}">{_escape(text) or "&nbsp;"}</span>'


def _changed_pair(old, new):
    # Compare characters before escaping them. Boundary removal only reduces
    # matcher work; every character is still emitted by the normal renderer.
    start = 0
    while start < min(len(old), len(new)) and old[start] == new[start]:
        start += 1
    old_end, new_end = len(old), len(new)
    while old_end > start and new_end > start and old[old_end-1] == new[new_end-1]:
        old_end -= 1
        new_end -= 1
    left, right = [_escape(old[:start])], [_escape(new[:start])]
    a, b = old[start:old_end], new[start:new_end]
    matcher = difflib.SequenceMatcher(difflib.IS_CHARACTER_JUNK, a, b, autojunk=False)
    for tag, i, j, k, l in matcher.get_opcodes():
        if tag == "equal":
            left.append(_escape(a[i:j])); right.append(_escape(b[k:l]))
        else:
            if i != j:
                left.append(_highlight(a[i:j], "chg" if tag == "replace" else "sub"))
            if k != l:
                right.append(_highlight(b[k:l], "chg" if tag == "replace" else "add"))
    left.append(_escape(old[old_end:])); right.append(_escape(new[new_end:]))
    return "".join(left), "".join(right)


def _runs(lines, start, end):
    while start < end:
        stop = start + 1
        while stop < end and lines[stop] == lines[start]:
            stop += 1
        yield lines[start], start, stop
        start = stop


def _run_anchor(old, new, a, b, c, d):
    """Find a verified long equal run without enumerating repeated-line pairs.

    One longest run per value is indexed. Equal-length candidates prefer similar
    relative positions. Gaps are compared again, so split/merged runs do not
    force their remaining equal lines to become artificial inserts/deletes.
    """
    indexed = {}
    for value, i, j in _runs(new, c, d):
        if j-i >= 32 and (value not in indexed or j-i > indexed[value][1]-indexed[value][0]):
            indexed[value] = (i,j)
    best = None; score = None
    for value, i, j in _runs(old, a, b):
        other = indexed.get(value)
        if j-i < 32 or other is None:
            continue
        k,l = other
        size = min(j-i,l-k)
        relative = abs((i-a)/(b-a) - (k-c)/(d-c))
        balance = abs((i-a+size/2)/(b-a)-0.5) + abs((k-c+size/2)/(d-c)-0.5)
        candidate = (size, -relative, -balance)
        if score is None or candidate > score:
            # Dictionary equality is full string equality, not a digest match.
            best, score = (i,i+size,k,k+size), candidate
    return best


def _line_opcodes(old, new):
    """Monotone, complete opcodes. Statistics continue to use independent LCS."""
    pending = [('compare',0,len(old),0,len(new))]
    while pending:
        tag,a,b,c,d = pending.pop()
        if tag != 'compare':
            if a != b or c != d: yield (tag,a,b,c,d)
            continue
        i,k=a,c
        while i < b and k < d and old[i] == new[k]: i+=1; k+=1
        if i>a: yield ('equal',a,i,c,k)
        a,c=i,k
        j,l=b,d
        while j>a and l>c and old[j-1] == new[l-1]: j-=1; l-=1
        if j<b: pending.append(('equal',j,b,l,d))
        b,d=j,l
        if a==b or c==d:
            if a!=b or c!=d: yield ('insert' if a==b else 'delete',a,b,c,d)
            continue
        anchor = _run_anchor(old,new,a,b,c,d) if min(b-a,d-c)>=128 else None
        if anchor is not None:
            i,j,k,l = anchor
            pending.extend([('compare',j,b,l,d), ('equal',i,j,k,l), ('compare',a,i,c,k)])
            continue
        matcher=difflib.SequenceMatcher(None,old[a:b],new[c:d],autojunk=False)
        for tag,i,j,k,l in matcher.get_opcodes():
            yield (tag,a+i,a+j,c+k,c+l)


def _line_groups(old, new, context, numlines):
    # Coalesce adjacent pieces before applying context. Only genuinely equal
    # intervals may be hidden; neither limits nor truncation are introduced.
    codes=[]
    for code in _line_opcodes(old,new):
        tag,i,j,k,l=code
        if codes and codes[-1][0]==tag and codes[-1][2]==i and codes[-1][4]==k:
            previous=codes[-1]; codes[-1]=(tag,previous[1],j,previous[3],l)
        else: codes.append(code)
    if not context:
        yield codes
        return
    if not any(code[0]!='equal' for code in codes): return
    if codes and codes[0][0]=='equal':
        tag,i,j,k,l=codes[0];codes[0]=(tag,max(i,j-numlines),j,max(k,l-numlines),l)
    if codes and codes[-1][0]=='equal':
        tag,i,j,k,l=codes[-1];codes[-1]=(tag,i,min(j,i+numlines),k,min(l,k+numlines))
    group=[]
    for tag,i,j,k,l in codes:
        if tag=='equal' and j-i>2*numlines:
            if numlines: group.append((tag,i,i+numlines,k,k+numlines))
            if group: yield group
            group=[];i=j-numlines;k=l-numlines
        if i!=j or k!=l: group.append((tag,i,j,k,l))
    if group: yield group


def make_table(old_lines, new_lines, fromdesc="", todesc="", context=False, numlines=3):
    """Compatibility entry: callers without a store still receive a string."""
    return "".join(iter_table(old_lines, new_lines, fromdesc, todesc, context, numlines))


def iter_table(old_lines, new_lines, fromdesc="", todesc="", context=False, numlines=3):
    # 复用标准库的 tab 表示，避免把原文 tab 与等宽空格误认为相同内容。
    old, new = difflib.HtmlDiff(tabsize=4)._tab_newline_replace(old_lines, new_lines)
    groups = _line_groups(old, new, context, numlines)
    prefix = "stable_" + uuid4().hex
    yield ('<div class="diff-render-note">使用完整逐行对比：替换段按行序对齐，保留行内差异。'
              + ('当前按所选设置显示差异上下文。' if context else '已显示全部内容。') + '</div>')
    yield (f'<table class="diff" id="{prefix}"><thead><tr>'
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
        yield ('<tbody>')
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
                yield ('<tr><td class="diff_next"></td>' + cell(a, left, "old")
                          + '<td class="diff_next"></td>' + cell(b, right, "new") + '</tr>\n')
                wrote_rows = True
        yield ('</tbody>')
    if not wrote_rows:
        yield ('<tbody><tr><td colspan="6">'
                  + ('没有内容差异' if context else '空文件') + '</td></tr></tbody>')
    yield ('</table>')


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
