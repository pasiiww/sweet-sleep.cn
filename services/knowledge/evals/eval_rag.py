#!/usr/bin/env python3
"""Offline retrieval evaluation for curated QQ questions; never calls a model."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import entities

HERE = Path(__file__).resolve().parent


def load_cases():
    return json.loads((HERE / 'rag_cases.json').read_text())['cases']


def fixture():
    temp = tempfile.TemporaryDirectory()
    server.DATA = Path(temp.name)
    server.initialize()
    kb = server.api('POST', '/knowledge/api/bases', {'name': '脱敏 RAG 测评库'}, {})['id']
    corpus = json.loads((HERE / 'rag_fixture.json').read_text())
    server.api('PUT', f'/knowledge/api/bases/{kb}/entities', {'items': corpus['entities']}, {})
    with server.db() as conn:
        for question in corpus['qa_questions']:
            server.save_qa(conn, kb, {'question': question, 'answer': '脱敏占位答案，仅用于评估召回排序。'})
    for document in corpus['documents']:
        server.api('POST', f'/knowledge/api/bases/{kb}/documents', document, {})
    return temp, kb


def key(row):
    return ('qa', row['question']) if row['source_type'] == 'qa' else ('document', row['title'])


def evaluate(kb, cases=None, top_k=5):
    cases = cases or load_cases()
    with server.db() as conn:
        catalog = entities.Catalog(server.entity_catalog(conn, kb))
    result = []
    for case in cases:
        found = server.search_terms(kb, [case['query']], catalog=catalog)['results']
        ranks = {key(row): rank for rank, row in enumerate(found, 1)}
        gold = [('qa', question) for question in case.get('gold_qa', [])]
        gold += [('document', title) for title in case.get('gold_documents', [])]
        forbidden = [('qa', question) for question in case.get('avoid_qa', [])]
        entry = {'id': case['id'], 'kind': case['kind'], 'query': case['query'],
                 'gold_ranks': [ranks.get(item) for item in gold],
                 'forbidden_ranks': [ranks.get(item) for item in forbidden],
                 'top': [key(row) for row in found[:top_k]]}
        result.append(entry)
    positive = [item for item in result if item['kind'] == 'answer']
    negative = [item for item in result if item['kind'] == 'handoff']
    all_at_5 = sum(all(rank is not None and rank <= top_k for rank in item['gold_ranks']) for item in positive)
    any_at_1 = sum(1 in item['gold_ranks'] for item in positive)
    wrong_at_5 = sum(any(rank is not None and rank <= top_k for rank in item['forbidden_ranks']) for item in negative)
    return {'summary': {'positive': len(positive), 'negative': len(negative),
                        'all_gold_at_5': all_at_5, 'any_gold_at_1': any_at_1,
                        'forbidden_at_5': wrong_at_5}, 'cases': result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, help='本地 SQLite 快照；省略时用脱敏内置语料')
    parser.add_argument('--kb-id', default='', help='快照中的知识库 ID；默认选择 QA 最多的库')
    parser.add_argument('--json', action='store_true', help='输出全部案例')
    args = parser.parse_args()
    temp = None
    try:
        if args.db:
            if args.db.name != 'knowledge.db':
                parser.error('快照文件必须命名为 knowledge.db，并放在独立目录')
            server.DATA = args.db.resolve().parent
            with server.db() as conn:
                kb = args.kb_id or conn.execute("SELECT kb_id FROM qa_entries GROUP BY kb_id ORDER BY count(*) DESC LIMIT 1").fetchone()[0]
        else:
            temp, kb = fixture()
        report = evaluate(kb)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(report['summary'], ensure_ascii=False))
            for item in report['cases']:
                print(f"{item['id']}: gold={item['gold_ranks']} avoid={item['forbidden_ranks']}")
    finally:
        if temp:
            temp.cleanup()


if __name__ == '__main__':
    main()
