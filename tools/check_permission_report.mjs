// 在真实浏览器中验证分类、筛选、详情与交付清单的边界。
import assert from 'node:assert/strict';

export async function checkPermissionReport(evaluate, click) {
    const counts = await evaluate(`(() => {
        const count = permissionData.length;
        if (Number(document.querySelector('.card.permission .num').textContent) !== count)
            throw Error('权限统计与完整记录不一致');
        if (manifestData.some(f => f.type === 'P') || fileData.some(f => f.type === 'P'))
            throw Error('权限记录混入交付集合');
        if (treeFileData.length !== fileData.length + count) throw Error('报告记录缺失或重复');
        if (!Object.values(filterState).every(Boolean)) throw Error('打开报告应默认显示全部类型');
        if (!document.querySelector('.flt.all.on')) throw Error('默认全部按钮未选中');
        if (document.querySelector('.flt.content')) throw Error('不应保留额外快捷筛选');
        if (Number(document.querySelector('.card.total .num').textContent) !== treeFileData.length)
            throw Error('变更文件总数与完整分类不一致');
        toggleAll(true);
        const initial = document.querySelectorAll('#tree [data-file-path]').length;
        if (initial !== treeFileData.length) throw Error('默认文件树错误');
        const identities = permissionData.map(f => (f.project || '') + '/' + f.path);
        if (new Set(identities).size !== count) throw Error('权限记录身份重复');
        for (let mask = 0; mask < 64; mask++) {
            ['A','M','F','D','R','P'].forEach((t,i) => filterState[t] = !!(mask & (1 << i)));
            applyFilter(); toggleAll(true);
            const expected = treeFileData.filter(f => filterState[f.type]).length;
            if (document.querySelectorAll('#tree [data-file-path]').length !== expected)
                throw Error('筛选组合丢失或重复路径: ' + mask);
        }
        const onlyP = buildTree([{path:'permissions/sub/file', displayPath:'permissions/sub/file', type:'P'}]);
        if (getDirType(onlyP.children.permissions) !== 'P') throw Error('权限目录标签错误');
        const mixed = buildTree([{path:'mixed/a', displayPath:'mixed/a', type:'P'},
                                {path:'mixed/b', displayPath:'mixed/b', type:'M'}]);
        if (getDirType(mixed.children.mixed) !== '') throw Error('混合目录被误标为纯权限');
        setFilter('all'); toggleAll(true);
        if (document.querySelectorAll('#tree .permission-badge').length !== fileData.filter(f => f.permissionChanged).length)
            throw Error('伴随权限标记丢失或重复');
        return {content: fileData.length, permissionOnly: count, filterCombinations: 64};
    })()`);
    if (counts.permissionOnly) {
        await click('#tree [data-file-type="P"]');
        assert.equal(await evaluate(`(() => {
            const panel = document.querySelector('#diffContainer .permission-detail');
            const path = document.querySelector('#tree .row.active').dataset.filePath;
            const record = permissionData.find(f => f.path === path);
            const values = panel ? [...panel.querySelectorAll('dd')].map(e => e.textContent) : [];
            return !!panel && panel.textContent.includes('逐字节相同') && panel.textContent.includes('无需复制') &&
                values.length === 2 && values[0] === record.oldMode && values[1] === record.newMode &&
                !document.querySelector('#diffContainer table.diff') &&
                !document.getElementById('diffNavBar').classList.contains('active');
        })()`), true);
        await click('.flt.p');
        assert.equal(await evaluate("document.querySelectorAll('#tree [data-file-type=\"P\"]').length"), 0);
        await click('.flt.all');
    }
    await evaluate(`(() => {
        for (const file of fileData.slice(0, 20)) {
            showDiff(file);
            if (!document.querySelector('#diffContainer .diff-panel')) throw Error('内容详情丢失');
            const nested = document.querySelector('#diffContainer .permission-changes');
            if (nested && (!nested.textContent.includes('P') || !nested.textContent.includes('不单独导出')))
                throw Error('包内权限标记或交付边界不明确');
            if (!file.archiveDetails && !document.getElementById('diffNavBar').classList.contains('active'))
                throw Error('切回内容后导航未恢复');
        }
        let text = '';
        const original = window.open;
        window.open = () => ({document:{open(){},write(){},close(){},getElementById(){return {set textContent(v){text=v;}}}}});
        try { openSummary(); } finally { window.open = original; }
        for (const file of permissionData) {
            const p = typeof summaryPath === 'function' ? summaryPath(file) : file.path;
            if (!text.includes(p + '：' + file.oldMode + ' → ' + file.newMode)) throw Error('权限清单不完整');
        }
        if (permissionData.length && !text.includes('无需复制文件内容')) throw Error('权限清单缺少交付说明');
        if (typeof window.__reportXss !== 'undefined') throw Error('不可信路径执行了脚本');
        setFilter('all');
        if (permissionData.length) toggleFilter('P');
        toggleAll(true);
        if (document.querySelectorAll('#tree [data-file-path]').length !== fileData.length)
            throw Error('关闭权限类型后仍显示权限文件');
        if (permissionData.length) { setFilter('all'); toggleAll(true); showDiff(permissionData[0]); }
    })()`);
    return {...counts, permissionDetail: counts.permissionOnly > 0, manifest: true, contentNavigation: counts.content > 0};
}
