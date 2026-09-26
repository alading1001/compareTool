"""Exercise the shipped event handlers with DOM doubles (not a browser/layout test)."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')

EVENT_CHECK = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync(process.argv[1], 'utf8');
const handler = template.match(/function handleDiffHorizontalKeydown\(event\) \{[\s\S]*?\n\}/)[0];
const context = vm.createContext({});
vm.runInContext(handler + '\n' + fs.readFileSync(process.argv[2], 'utf8'), context);
let active;
class Element {
    constructor(parent = null) {
        this.parent = parent; this.listeners = {}; this.attrs = {};
        this.scrollLeft = 0; this.scrollWidth = 1600; this.clientWidth = 400;
    }
    addEventListener(type, listener) {
        (this.listeners[type] ||= []).push(listener);
    }
    setAttribute(name, value) { this.attrs[name] = value; }
    focus(options) { active = this; this.focusOptions = options; }
    scrollBy({left}) {
        this.scrollLeft = Math.max(0, Math.min(this.scrollWidth - this.clientWidth,
            this.scrollLeft + left));
    }
    emit(type, fields = {}) {
        const event = Object.assign({defaultPrevented: false,
            preventDefault() { this.defaultPrevented = true; },
            stopPropagation() { this.stopped = true; }}, fields);
        this.dispatch(type, event);
        return event;
    }
    dispatch(type, event) {
        event.currentTarget = this;
        for (const listener of this.listeners[type] || []) listener(event);
        if (this.parent && !event.stopped) this.parent.dispatch(type, event);
    }
}
const root = new Element();
const panel = new Element();
panel.addEventListener('keydown', context.handleDiffHorizontalKeydown);
context.initArchiveDetails(root);
function member() {
    const leaf = new Element(panel), nav = new Element(panel), label = {};
    const buttons = [-1, 1].map(step => {
        const button = new Element(nav);
        button.dataset = {archiveStep: String(step)};
        return button;
    });
    const rows = [true, false, true].map(changed => ({
        marked: false,
        querySelector: () => changed,
        classList: {add() {}, remove() {}},
        scrollIntoView() {},
    }));
    leaf.querySelectorAll = selector => selector === 'table.diff tr' ? rows : [];
    nav.querySelector = () => label;
    nav.querySelectorAll = () => buttons;
    const body = {appendChild() {}, querySelector: selector =>
        selector.endsWith('.archive-leaf') ? leaf : nav};
    const template = {content: {cloneNode() {}}, remove() {}};
    const item = {open: true, matches: () => true, querySelector: selector =>
        selector.includes('template') ? template : body};
    root.emit('toggle', {target: item});
    return {leaf, buttons, label, item};
}
const first = member(), second = member();
assert.equal(first.leaf.tabIndex, 0, '正文须能用 Tab 聚焦');
first.buttons[1].focus();
first.buttons[1].emit('click', {detail: 1});
assert.equal(first.label.textContent, '1 / 2');
assert.equal(active, first.leaf, '鼠标定位后应聚焦包内正文');
assert.equal(active.focusOptions.preventScroll, true);
first.leaf.emit('keydown', {key: 'ArrowRight'});
assert.equal(first.leaf.scrollLeft, 80, '右键应移动当前成员');
assert.equal(panel.scrollLeft, 0, '同一次右键不能再滚动外层面板');
assert.equal(second.leaf.scrollLeft, 0, '不能滚动其他成员');
first.leaf.emit('keydown', {key: 'ArrowLeft'});
assert.equal(first.leaf.scrollLeft, 0, '左键应恢复当前成员位置');
assert.equal(first.leaf.emit('keydown', {key: 'ArrowLeft'}).defaultPrevented, true);
assert.equal(panel.scrollLeft, 0, '到达成员边缘时仍不能串到外层');
for (const modifier of ['altKey', 'ctrlKey', 'metaKey', 'shiftKey']) {
    const event = first.leaf.emit('keydown', {key: 'ArrowRight', [modifier]: true});
    assert.equal(event.defaultPrevented, false, '保留组合键行为');
}
assert.equal(first.leaf.scrollLeft, 0);
first.buttons[1].focus();
first.buttons[1].emit('click', {detail: 0});
assert.equal(active, first.buttons[1], '键盘定位不能抢走按钮焦点');
assert.equal(first.label.textContent, '2 / 2');
root.emit('toggle', {target: first.item});
first.leaf.emit('keydown', {key: 'ArrowRight'});
assert.equal(first.leaf.scrollLeft, 80, '再次展开不能重复绑定按键');
second.buttons[0].emit('click', {detail: 1});
assert.equal(active, second.leaf);
second.leaf.emit('keydown', {key: 'ArrowRight'});
assert.equal(second.leaf.scrollLeft, 80);
assert.equal(first.leaf.scrollLeft, 80, '成员之间的焦点和滚动应独立');
panel.emit('keydown', {key: 'ArrowRight'});
assert.equal(panel.scrollLeft, 80, '普通文件的外层方向键处理仍可用');
"""


@unittest.skipUnless(NODE, 'Node.js is required for JavaScript event regression checks')
class ArchiveKeyboardNavigationTests(unittest.TestCase):
    def test_single_and_multi_report_keyboard_events(self):
        for name in ('report.html', 'multi_report.html'):
            with self.subTest(template=name):
                result = subprocess.run(
                    [NODE, '-e', EVENT_CHECK, str(ROOT / 'templates' / name),
                     str(ROOT / 'templates' / 'archive_script.html')],
                    capture_output=True, text=True, encoding='utf-8', timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
