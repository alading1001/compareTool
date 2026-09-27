"""Complete opcodes, readable common blocks and actual renderer route coverage."""
import random
import difflib
import unittest
from unittest import mock
import diff_engine
from diff_engine import DiffEngine
from stable_diff import _line_groups, _line_opcodes, _run_anchor, make_table
from test_complete_export_review_fixes import BytesVCS, TableRows

class MatcherHotspotTests(unittest.TestCase):
    def check_codes(self,old,new):
        i=k=0
        for tag,a,b,c,d in _line_opcodes(old,new):
            self.assertEqual((i,k),(a,c))
            self.assertTrue(a<=b<=len(old) and c<=d<=len(new))
            self.assertTrue(a!=b or c!=d)
            if tag=='equal': self.assertEqual(old[a:b],new[c:d])
            elif tag=='insert': self.assertEqual(a,b)
            elif tag=='delete': self.assertEqual(c,d)
            else: self.assertEqual('replace',tag)
            i,k=b,d
        self.assertEqual((len(old),len(new)),(i,k))

    def test_repeated_blocks_shift_split_merge_and_context(self):
        cases=[(['old-start']*64+['repeat']*n+['old-end']*64,
                ['new-start']*97+['repeat']*(n+17)+['new-end']*71) for n in (200,400,800)]
        old=['old-start']*64+['repeat']*400+['deleted']+['repeat']*400+['old-end']*64
        new=['new-start']*64+['repeat']*800+['new-end']*64
        cases.extend([(old,new),(new,old)])
        for old,new in cases:
            with self.subTest(length=len(old)):
                self.check_codes(old,new)
                codes=list(_line_opcodes(old,new))
                equal=sum(b-a for tag,a,b,c,d in codes if tag=='equal')
                self.assertEqual(min(old.count('repeat'),new.count('repeat')),equal)
                rows=TableRows(make_table(old,new))
                self.assertEqual(list(enumerate(old,1)),rows.side())
                self.assertEqual(list(enumerate(new,1)),rows.side(True))
                for n in (0,1,3,20):
                    for group in _line_groups(old,new,True,n):
                        for tag,a,b,c,d in group:
                            if tag=='equal':self.assertEqual(old[a:b],new[c:d])
                    context=TableRows(make_table(old,new,context=True,numlines=n))
                    for number,text in context.side(): self.assertEqual(old[number-1],text)
                    for number,text in context.side(True): self.assertEqual(new[number-1],text)

    def test_random_complete_partition(self):
        randomizer=random.Random(20260926)
        for _ in range(300):
            old=[randomizer.choice(['a','b','x y','x\ty','中<&>']) for _ in range(randomizer.randrange(40))]
            new=[randomizer.choice(['a','b','x y','x\ty','中<&>']) for _ in range(randomizer.randrange(40))]
            self.check_codes(old,new)
        for _ in range(50):
            old=sum(([randomizer.choice('abc')]*randomizer.randrange(1,100) for _ in range(8)),[])
            new=sum(([randomizer.choice('abc')]*randomizer.randrange(1,100) for _ in range(8)),[])
            self.check_codes(old,new)

    def test_moved_runs_compete_with_common_blocks_by_line_count(self):
        # Neither a repeated run nor a unique anchor always wins. Keep the
        # larger contiguous block, including ordinary code with blank lines.
        for common_size, repeated in ((2000, 32), (700, 80), (96, 400)):
            for blank_lines in (False, True):
                common = [f'unchanged_{i}' for i in range(common_size)]
                if blank_lines:
                    common = [line for value in common for line in (value, '')]
                old = ['old-start'] * 64 + common + ['repeat'] * repeated + ['old-end'] * 64
                new = ['new-start'] * 64 + ['repeat'] * repeated + common + ['new-end'] * 64
                for left, right in ((old, new), (new, old)):
                    with self.subTest(common=len(common), repeated=repeated, reverse=left is new):
                        self.check_codes(left, right)
                        equal = [line for tag, a, b, c, d in _line_opcodes(left, right)
                                 if tag == 'equal' for line in left[a:b]]
                        self.assertEqual(common if len(common) > repeated else ['repeat'] * repeated,
                                         equal)

    def test_run_boundaries_find_an_actual_longest_common_block(self):
        randomizer = random.Random(20260927)
        for _ in range(120):
            old = ['shared'] * 32 + sum(([randomizer.choice('abc')] * randomizer.randrange(1, 65)
                                        for _ in range(8)), [])
            new = sum(([randomizer.choice('abc')] * randomizer.randrange(1, 65)
                       for _ in range(8)), []) + ['shared'] * 32
            i, j, k, l = _run_anchor(old, new, 0, len(old), 0, len(new))
            self.assertEqual(old[i:j], new[k:l])
            reference = difflib.SequenceMatcher(None, old, new, autojunk=False).find_longest_match()
            self.assertEqual(reference.size, j-i)

    def test_single_few_and_batch_long_lines_in_real_engine(self):
        for count in (1,5,64):
            old=['ab'*1000+'X']+['old-row']*(count-1)
            new=['ab'*1000+'Y']+['new-row']*(count-1)
            with mock.patch('diff_engine.make_stable_diff_table',wraps=diff_engine.make_stable_diff_table) as stable:
                result=DiffEngine(BytesVCS(('\n'.join(old)).encode(),('\n'.join(new)).encode())).generate_diff('old','new')
                self.assertEqual(count>=64,stable.called)
            rows=TableRows(result.files[0].side_by_side_html)
            self.assertEqual(list(enumerate(old,1)),rows.side())
            self.assertEqual(list(enumerate(new,1)),rows.side(True))
            self.assertEqual((count,count),(result.files[0].deleted_lines,result.files[0].added_lines))

if __name__=='__main__':unittest.main()
