"""Bounded write-time identity recall and knowledge-retention projection.

No historical pair scanner or standalone classifier. Reads never call models.
"""
from collections import defaultdict
import json
import re
import unicodedata
from .core import tokens

VERSION = 'condensation-v1'


def enabled(store):
    return store.config.get('graph_v2', {}).get('condensation', {}).get('enabled') is True


def protected(node):
    """Conservative numeric/version/split guard. Equality is NOT equivalence."""
    text = unicodedata.normalize('NFKC', node['title']+' '+node['content'])
    values = sorted(set(re.findall(r'[A-Za-z_][A-Za-z0-9_./:+-]*|[+−-]?\d+(?:\.\d+)?(?:[eE][+−-]?\d+)?%?', text)))
    qualifiers = sorted(set(re.findall(r'训练集|验证集|测试集|留出集|离线|在线|训练前|训练后|成功|失败|条件[：:]|如果|仅当|除非|否则|前提|空闲后|完成后|之前|之后|验证前|验证后', text)))
    return values, qualifiers


def coverage_compatible(a,b):
    """Conservative admission veto, NEVER an equivalence proof.

    A label cannot erase an extra proposition. Near-duplicate wording can enter
    classification; substantial unmatched content remains separate, even at the
    cost of false negatives. Titles remain in the protected-value guard and the
    full model comparison; a different display label is not itself new content.
    """
    def terms(n):
        return {t for t in tokens(n['content']) if len(t)>1}
    left,right=terms(a),terms(b)
    return len(left-right)<=4 and len(right-left)<=4


def payload(store, node_id):
    row = store.db.execute('''SELECT n.type,n.identity_json,m.* FROM sg2_nodes n
        JOIN work_memory m ON m.id=n.id WHERE n.id=? AND n.project=?''',
        (node_id,store.config['project_id'])).fetchone()
    if not row:
        return None
    # Match the current source-owned payload, not an arbitrary historical value.
    ev = store.db.execute('''SELECT assertion_json FROM sg2_evidence
        WHERE node=? AND entry_id=? AND quote=? ORDER BY id LIMIT 1''',
        (node_id,row['source_entry_id'],row['source_quote'])).fetchone()
    p = json.loads(ev[0]) if ev else {}
    return dict(p, id=node_id,type=row['type'],title=row['title'],content=row['content'],
                status=row['status'],identity=json.loads(row['identity_json']),
                aliases=json.loads(row['aliases_json']),evidence=[dict(entry_id=row['source_entry_id'],quote=row['source_quote'])])


def recall(store, node, limit=4):
    """Per-object lookup, not batch top12 or a title-only admission gate.

    A shared source finds reworded legacy facts with no stable key. Per-object
    terms find cross-message paraphrases. Return an overflow flag: an incomplete
    search that found only different candidates is NOT proof of novelty.
    """
    from .graph_compaction import expression
    from .graph_v2 import _conflict
    ids, scores = set(), defaultdict(int)
    cid = expression(store,'x.node')
    for ev in node['evidence']:
        for row in store.db.execute(f'''SELECT DISTINCT {cid} FROM sg2_evidence x
            JOIN sg2_nodes n ON n.id=x.node WHERE n.project=? AND n.type=? AND x.entry_id=? LIMIT 129''',
            (store.config['project_id'],node['type'],ev['entry_id'])):
            ids.add(row[0]);scores[row[0]] += 2
    words = sorted(set(tokens(node['title']+' '+node['content'])))[:120]
    if words:
        marks=','.join('?' for _ in words);ct=expression(store,'t.node')
        for row in store.db.execute(f'''SELECT {ct} id,count(DISTINCT t.term) score
            FROM sg2_terms t JOIN sg2_nodes n ON n.id={ct}
            WHERE t.project=? AND n.type=? AND t.term IN ({marks})
            GROUP BY 1 ORDER BY score DESC,id LIMIT 33''',
            (store.config['project_id'],node['type'],*words)):
            ids.add(row[0]);scores[row[0]] += row[1]
    incoming=set(tokens(node['content']))
    found=[]
    for identity in ids:
        p=payload(store,identity)
        if not p or _conflict(node,p):
            continue
        if node['type'] in {'Claim','Decision'} and (protected(node)!=protected(p) or not coverage_compatible(node,p)):
            continue
        overlap=incoming & set(tokens(p['content']))
        # Source co-occurrence alone is not semantic proximity. At least a useful
        # content term must overlap; identical content is always eligible.
        if node['content'] != p['content'] and len(overlap)<2:
            continue
        found.append((len(overlap),scores[identity],identity,p))
    found.sort(key=lambda x:(-x[0],-x[1],x[2]))
    return [x[3] for x in found[:limit]],len(found)>limit


def project_output(output, stats):
    """Extractor retention classification is audited in the original receipt.

    Routine-only candidates stay raw-searchable, not another knowledge object.
    References from retained knowledge to these candidates stay in the receipt, never
    become invented identity aliases or silently accepted relationships.
    """
    skipped={n['id'] for n in output['nodes'] if n.get('retention')=='record_only'}
    if not skipped:
        return output,set()
    result=dict(output,nodes=[n for n in output['nodes'] if n['id'] not in skipped])
    # Only a relationship to actual retained/existing knowledge is meaningful.
    # The complete original relationship remains in the immutable job receipt.
    result['relations']=[r for r in output['relations'] if r['source'] not in skipped and r['target'] not in skipped]
    stats['routine_nodes_skipped']+=len(skipped)
    if not result['nodes'] and not result['relations']:
        result['coverage']=dict(output['coverage'],outcome='no_durable_item')
    return result,skipped
