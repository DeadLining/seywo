"""Deterministic canonical/episode projection without destructive compaction."""
import hashlib
import json

import pytest

from sessiongraph import condensation as c, graph_v2 as g, tiering
from sessiongraph.core import Store, load_config
from sessiongraph.graph_contract import VERSION


@pytest.fixture
def env(tmp_path):
    from test_memories import make_store, entries
    _, store, _ = make_store(tmp_path, [
        ('assistant', '误差数值为1。运行一与运行二不同。'),
        ('assistant', '误差数值为2。运行一与运行二不同。'),
    ])
    store.config['graph_v2'] = dict(enabled=True, condensation=dict(enabled=True))
    g.setup(store)
    yield store, entries(store)
    store.close()


def node(entry, **extra):
    value = dict(id='n1', type='Claim', title='核验结果', content='误差数值为1。',
                 status='completed', aliases=[], identity=[],
                 evidence=[dict(entry_id=entry['id'], quote=entry['text'])])
    value.update(extra)
    return value


def publish(store, dbid, entry, **extra):
    n = node(entry, **extra)
    job = 'job-' + dbid
    with store.db:
        store.db.execute('INSERT INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                         (job, store.config['project_id'], 'chat-1', 'chat-1', 'judging',
                          '{}', None, '[]', '{}', None, '', ''))
    g._commit(store, dict(id=job, chat='chat-1'),
              dict(nodes=[n], relations=[], unresolved=[]), [],
              dict(n1=dbid), [], [], [], g._stats(), (90, 0), finalize=False)
    return n


def test_preview_is_read_only_and_does_not_add_mapping_rows(env):
    store, _ = env
    before = store.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0]
    rows = store.db.execute('SELECT count(*) FROM sg_node_tiers').fetchone()[0]
    result = tiering.run(store, apply=False)
    assert result['applied'] is False and result['readonly'] is True
    assert result['model_calls'] == 0
    assert store.db.execute('SELECT count(*) FROM sg_node_tiers').fetchone()[0] == rows
    assert store.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == before


def test_new_publication_tiers_inside_the_existing_transaction(env):
    store, entries = env
    publish(store, 'a', entries[0], retention='knowledge',
            assertion=[dict(subject='误差', predicate='数值', value='1', unit='none', conditions=[])])
    row = store.db.execute('SELECT tier,reason,identity_key,canonical_id FROM sg_node_tiers WHERE node=?', ('a',)).fetchone()
    assert tuple(row)[:2] == ('canonical', 'atomic_assertion')
    assert row['identity_key'] and row['canonical_id'] == 'a'


def test_no_atomic_or_strong_identity_is_episode_not_fabricated(env):
    store, entries = env
    publish(store, 'a', entries[0], retention='knowledge', status='proposed')
    row = store.db.execute('SELECT tier,reason,identity_key FROM sg_node_tiers WHERE node=?', ('a',)).fetchone()
    assert tuple(row) == ('episode', 'atomic_assertion_missing', '')


def test_process_record_strong_identity_stays_episode(env):
    store, entries = env
    publish(store, 'a', entries[0], type='Execution', retention='knowledge',
            identity=[dict(namespace='run_id', value='run-1', scope='project')])
    row = store.db.execute('SELECT tier,reason FROM sg_node_tiers WHERE node=?', ('a',)).fetchone()
    assert tuple(row) == ('episode', 'process_record')
    assert store.db.execute('SELECT count(*) FROM sg2_nodes').fetchone()[0] == 1

def test_terminal_knowledge_without_atomic_assertion_remains_canonical(env):
    store, entries = env
    publish(store, 'a', entries[0], retention='knowledge', status='completed')
    row = store.db.execute('SELECT tier,reason FROM sg_node_tiers WHERE node=?', ('a',)).fetchone()
    assert tuple(row) == ('canonical', 'knowledge_retention')
    publish(store, 'b', entries[1], retention='knowledge', status='proposed')
    row = store.db.execute('SELECT tier,reason FROM sg_node_tiers WHERE node=?', ('b',)).fetchone()
    assert tuple(row) == ('episode', 'atomic_assertion_missing')

def test_record_only_never_enters_canonical_and_apply_keeps_all_facts(env):
    store, entries = env
    publish(store, 'a', entries[0])
    store.db.execute("UPDATE sg2_evidence SET assertion_json=? WHERE node='a'", (json.dumps({'retention': 'record_only'}),))
    store.db.commit()
    publish(store, 'b', entries[1], retention='knowledge')
    before = {table: store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
              for table in ('work_memory', 'sg2_nodes', 'sg2_evidence')}
    result = tiering.run(store, apply=True)
    after = {table: store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
             for table in before}
    assert before == after
    assert result['applied'] and result['graph_facts_changed'] is False
    assert result['model_calls'] == 0
    assert store.db.execute('SELECT tier FROM sg_node_tiers WHERE node=?', ('a',)).fetchone()[0] == 'record_only'


def _table_digest(store, table):
    columns = [row['name'] for row in store.db.execute(f'PRAGMA table_info({table})')]
    names = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
    rows = [list(row) for row in store.db.execute(f'SELECT {names} FROM {table}')]
    rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
    payload = json.dumps(rows, ensure_ascii=False, separators=(',', ':'), default=str)
    return len(rows), hashlib.sha256(payload.encode()).hexdigest()


def test_apply_changes_only_the_shadow_mapping(env):
    store, entries = env
    publish(store, 'a', entries[0], retention='knowledge',
            assertion=[dict(subject='误差', predicate='数值', value='1', unit='none', conditions=[])])
    publish(store, 'b', entries[1], retention='knowledge', status='proposed')
    tables = ('work_memory', 'sg2_nodes', 'sg2_evidence', 'sg2_relations')
    before = {table: _table_digest(store, table) for table in tables}
    result = tiering.run(store, apply=True)
    after = {table: _table_digest(store, table) for table in tables}
    assert before == after
    assert result['applied'] is True
    assert result['graph_facts_changed'] is False
    assert result['evidence_changed'] is False
    assert result['model_calls'] == 0
    assert store.db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    assert not list(store.db.execute('PRAGMA foreign_key_check'))


def test_setup_never_relabels_existing_rows_as_the_current_rules(env):
    store, entries = env
    publish(store, 'a', entries[0], retention='knowledge')
    publish(store, 'b', entries[1], retention='knowledge')
    with store.db:
        store.db.execute("UPDATE sg_node_tiers SET revision='tiering-v1'")
        store.db.execute("DELETE FROM metadata WHERE key='tiering_revision'")
    tiering.setup(store)
    report = tiering.summary(store)
    assert report['revision'] == 'tiering-v1'
    assert report['revisions'] == {'tiering-v1': 2}
    assert report['covered'] == report['total'] == 2 and report['complete'] is True
    assert report['current'] is False
    assert not store.db.execute(
        "SELECT 1 FROM metadata WHERE key='tiering_revision'").fetchone()


def test_bounded_backfill_reports_mixed_revision_until_whole_project_is_current(env):
    store, entries = env
    publish(store, 'a', entries[0], retention='knowledge')
    publish(store, 'b', entries[1], retention='knowledge')
    with store.db:
        store.db.execute("UPDATE sg_node_tiers SET revision='tiering-v1'")
        store.db.execute("DELETE FROM metadata WHERE key='tiering_revision'")
    first = tiering.run(store, apply=True, limit=1)
    partial = tiering.summary(store)
    assert first['scanned'] == 1
    assert partial['revision'] == 'mixed'
    assert partial['revisions'] == {tiering.VERSION: 1, 'tiering-v1': 1}
    assert partial['current'] is False
    assert not store.db.execute(
        "SELECT 1 FROM metadata WHERE key='tiering_revision'").fetchone()
    tiering.run(store, apply=True)
    complete = tiering.summary(store)
    assert complete['revision'] == tiering.VERSION
    assert complete['revisions'] == {tiering.VERSION: 2}
    assert complete['complete'] is True
    assert complete['current'] is True
    assert store.db.execute(
        "SELECT value FROM metadata WHERE key='tiering_revision'").fetchone()[0] == tiering.VERSION


def test_partial_coverage_is_not_current_even_when_every_row_uses_the_current_rules(env):
    store, entries = env
    publish(store, 'a', entries[0], retention='knowledge')
    publish(store, 'b', entries[1], retention='knowledge')
    tiering.run(store, apply=True)
    with store.db:
        store.db.execute("DELETE FROM sg_node_tiers WHERE node='b'")
    report = tiering.summary(store)
    assert report['revisions'] == {tiering.VERSION: 1}
    assert report['covered'] == 1 and report['total'] == 2
    assert report['complete'] is False and report['current'] is False


def test_different_value_is_not_aliased_and_yields_a_distinct_atomic_key(env):
    store, entries = env
    a = dict(type='Claim', identity=[], title='误差', content='误差数值为1。',
             evidence=[dict(entry_id=entries[0]['id'], quote=entries[0]['text'])],
             assertion=[dict(subject='误差', predicate='数值', value='1', unit='none', conditions=[])])
    b = {**a, 'content': '误差数值为2。', 'evidence': [dict(entry_id=entries[1]['id'], quote=entries[1]['text'])],
         'assertion': [dict(subject='误差', predicate='数值', value='2', unit='none', conditions=[])]}
    # The atomic lane deliberately excludes value. The value remains visible in
    # the complete assertion and therefore must produce different tier keys.
    ka = tiering.classify(dict(a, assertion_json=json.dumps(a), evidence_text=entries[0]['text'],
                               identity_json='[]', linked=1, type='Claim'))
    kb = tiering.classify(dict(b, assertion_json=json.dumps(b), evidence_text=entries[1]['text'],
                               identity_json='[]', linked=1, type='Claim'))
    assert ka[0] == kb[0] == 'canonical' and ka[2] != kb[2]


def test_unanchored_assertion_and_example_context_are_episode(env):
    store, entries = env
    bad = dict(type='Claim', title='误差', content='误差数值为1。', identity_json='[]', linked=1,
               assertion_json=json.dumps({'assertion': [dict(subject='不在原文', predicate='数值',
                                                             value='1', unit='none', conditions=[])]}),
               evidence_text=entries[0]['text'])
    example = dict(type='Claim', title='示例', content='示例', identity_json='[]', linked=1,
                   assertion_json=json.dumps({'context_kind': 'example'}),
                   evidence_text=entries[0]['text'])
    assert tiering.classify(bad)[:2] == ('episode', 'atomic_assertion_unanchored')
    assert tiering.classify(example)[:2] == ('episode', 'example_context')


def test_backfill_is_batched_idempotent_and_never_changes_evidence(env):
    store, entries = env
    publish(store, 'a', entries[0], retention='knowledge')
    publish(store, 'b', entries[1], retention='knowledge')
    evidence = store.db.execute('SELECT id,node,quote FROM sg2_evidence ORDER BY id').fetchall()
    first = tiering.run(store, apply=True, limit=1)
    second = tiering.run(store, apply=True)
    third = tiering.run(store, apply=True)
    assert first['scanned'] == 1 and second['scanned'] == 2 and third['scanned'] == 2
    assert store.db.execute('SELECT id,node,quote FROM sg2_evidence ORDER BY id').fetchall() == evidence
    assert store.db.execute('SELECT count(*) FROM sg_node_tiers').fetchone()[0] == 2


def test_shadow_table_cascades_with_node_and_never_blocks_delete(env):
    store, entries = env
    publish(store, 'a', entries[0], retention='knowledge')
    with store.db:
        store.db.execute('DELETE FROM sg2_evidence WHERE node=?', ('a',))
        store.db.execute('DELETE FROM sg2_relations WHERE source=? OR target=?', ('a', 'a'))
        store.db.execute('DELETE FROM sg2_keys WHERE node=?', ('a',))
        store.db.execute('DELETE FROM sg2_terms WHERE node=?', ('a',))
        store.db.execute('DELETE FROM sg2_nodes WHERE id=?', ('a',))
        store.db.execute('DELETE FROM work_memory WHERE id=?', ('a',))
    assert not store.db.execute('SELECT 1 FROM sg_node_tiers WHERE node=?', ('a',)).fetchone()

def test_relation_linked_artifact_is_reclassified_after_relations_publish(env):
    store, entries = env
    execution = dict(id='execution', type='Execution', title='训练执行', content='运行一与运行二不同。',
                     status='completed', aliases=[], identity=[],
                     evidence=[dict(entry_id=entries[0]['id'], quote=entries[0]['text'])],
                     retention='knowledge')
    artifact = dict(id='artifact', type='Artifact', title='核验结果', content='误差数值为1。',
                    status='completed', aliases=[], identity=[],
                    evidence=[dict(entry_id=entries[0]['id'], quote=entries[0]['text'])],
                    retention='knowledge')
    relation = dict(source='execution', target='artifact', kind='PRODUCES', scope='该次执行',
                    statement_mode='asserted',
                    evidence=[dict(entry_id=entries[0]['id'], quote=entries[0]['text'])])
    job = 'job-relation-linked'
    with store.db:
        store.db.execute('INSERT INTO sg2_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                         (job, store.config['project_id'], 'chat-1', 'chat-1', 'judging',
                          '{}', None, '[]', '{}', None, '', ''))
    g._commit(store, dict(id=job, chat='chat-1', stats_json='{}', segment='chat-1'),
              dict(nodes=[execution, artifact], relations=[relation], unresolved=[]), [],
              dict(execution='execution', artifact='artifact'), [], [(relation, 'asserted', {})],
              [], g._stats(), (120, 0), finalize=True)
    row = store.db.execute('SELECT tier,reason FROM sg_node_tiers WHERE node=?', ('artifact',)).fetchone()
    assert tuple(row) == ('canonical', 'knowledge_linked')

def test_work_map_views_filter_presentation_but_keep_all_facts_searchable(env):
    store, entries = env
    publish(store, 'canonical', entries[0], retention='knowledge',
            assertion=[dict(subject='误差', predicate='数值', value='1', unit='none', conditions=[])])
    publish(store, 'episode', entries[1], retention='knowledge', status='proposed')
    from sessiongraph.work_map import work_map
    all_map = work_map(store, view='all')
    knowledge = work_map(store, view='knowledge')
    episode = work_map(store, view='episode')
    all_ids = {n['id'] for n in all_map['nodes'] if n['type'] == 'memory'}
    knowledge_ids = {n['id'] for n in knowledge['nodes'] if n['type'] == 'memory'}
    episode_ids = {n['id'] for n in episode['nodes'] if n['type'] == 'memory'}
    assert {'canonical', 'episode'} <= all_ids
    assert 'canonical' in knowledge_ids and 'episode' not in knowledge_ids
    assert 'episode' in episode_ids and 'canonical' not in episode_ids
    assert knowledge['view'] == 'knowledge' and episode['view'] == 'episode'
    assert knowledge['tiering']['canonical'] == 1
    assert episode['tiering']['episode'] >= 1
    # Retrieval remains project-wide; tiering is a presentation projection only.
    from sessiongraph.views import memory_detail
    assert memory_detail(store, 'episode')['memory']['content']
    assert episode['model_calls'] == all_map['model_calls'] == 0


def test_pre_graph_work_memory_is_episode_and_never_disappears(tmp_path):
    from sessiongraph.memory import remember
    from test_memories import make_store
    from sessiongraph.work_map import work_map
    _, store, _ = make_store(tmp_path, [('assistant', '历史事项仍有原文证据')])
    eid = store.db.execute('SELECT id FROM entries LIMIT 1').fetchone()[0]
    mid = remember(store, kind='finding', title='历史事项', content='历史事项仍有原文证据',
                   source_entry_id=eid, source_quote='历史事项仍有原文证据')['id']
    assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='sg2_nodes'").fetchone()
    tiering.run(store, apply=True)
    assert store.db.execute('SELECT tier,reason FROM sg_node_tiers WHERE node=?',
                            (mid,)).fetchone()[:] == ('episode', 'pre_graph_memory')
    assert mid in {node['id'] for node in work_map(store, view='episode')['nodes']}
    assert mid not in {node['id'] for node in work_map(store, view='knowledge')['nodes']}


def test_work_map_knowledge_view_is_compatible_before_tier_backfill(tmp_path):
    from test_memories import make_store
    from sessiongraph.work_map import work_map
    _, store, _ = make_store(tmp_path, [('assistant', '兼容旧索引的事项')])
    assert not tiering.available(store)
    assert {n['id'] for n in work_map(store, view='knowledge')['nodes']} == {
        n['id'] for n in work_map(store, view='all')['nodes']}


def row_for(**extra):
    value = dict(id='r1', type='Claim', title='诊断对照结果', content='误差 0.1，仅限固定初态。',
                 status='completed', kind='finding', identity_json='[]', linked=1,
                 assertion_json=json.dumps({}), evidence_text='误差 0.1，仅限固定初态。',
                 source_payload_exact=True)
    value.update(extra)
    return value


def test_upload_receipt_is_episode_but_audited_bundle_is_canonical():
    receipt = row_for(type='Artifact', kind='artifact', title='eval_sources.tar.gz',
                      content='从本地上传到远端，sha256=' + 'a' * 64 + '，传输 4378 字节。')
    assert tiering.classify(receipt)[:2] == ('episode', 'transfer_receipt')
    bundle = row_for(type='Artifact', kind='artifact',
                     title='ankle_hold_audit_remote_bundle_20261019.zip',
                     content='踝关节对照远端审计证据压缩包：8个文件，含完整审计与 manifest，已下载。')
    assert tiering.classify(bundle)[0] == 'canonical'


def test_transient_artifacts_never_enter_knowledge():
    for title in ('training_finished.json', 'snapshot_096.json', 'status.json', 'progress_1.json'):
        assert tiering.classify(row_for(type='Artifact', kind='artifact', title=title))[0] == 'episode'


def test_terminal_scoped_diagnostic_is_knowledge_but_running_probe_is_not():
    detail = ('对照结果显示 warm 误差 0.1、cold 误差 0.3；仅限固定初态，不能外推到其他姿态。')
    done = row_for(type='Execution', kind='progress', title='固定初态动作误差诊断',
                   content=detail, status='completed')
    running = row_for(type='Execution', kind='progress', title='固定初态动作误差诊断',
                      content=detail, status='in_progress')
    assert tiering.classify(done)[:2] == ('canonical', 'execution_conclusion')
    assert tiering.classify(running)[:2] == ('episode', 'process_record')


def test_constrained_decision_is_knowledge_but_bare_next_step_is_not():
    rule = row_for(type='Decision', kind='decision', title='历史学生模型不得替换',
                   content='历史学生模型必须使用原 checkpoint，不得用不兼容模型替代。', status='active')
    plan = row_for(type='Decision', kind='decision', title='下一步：继续数据聚合',
                   content='下一步继续聚合数据并做一个新实验。', status='proposed')
    assert tiering.classify(rule)[:2] == ('canonical', 'decision_knowledge')
    assert tiering.classify(plan)[:2] == ('episode', 'plan_only')


def test_concrete_control_design_survives_a_next_step_title():
    design = row_for(
        type='Decision', kind='decision', status='proposed',
        title='下一步：实现300控制局部保持诊断',
        content=('单固定回合，固定初态；seed 2026101974；最多300环境交互、'
                 '1200个物理步、0优化、0重试；原门槛保持不变。'))
    # A fully specified control design is a protocol; both are durable
    # knowledge and the exact sub-reason is not part of the tier contract.
    assert tiering.classify(design)[0] == 'canonical'


def test_progress_specification_and_preregistered_protocol_are_knowledge():
    spec = row_for(
        type='Execution', kind='progress', title='14 维动作顺序与随机化配置解析核验',
        content=('解析出 resolved_target_ids [0..13] 与 14 个 actuator_names；'
                 '观测项和动作顺序在配置中固定。'))
    protocol = row_for(
        type='Execution', kind='progress', status='proposed',
        title='固定预算可学习性对照协议',
        content=('预注册对照协议：initialization_seeds [42,43]，max_updates 2000，'
                 'eval_every 100，batch 1024，lr 0.0002，从零训练，'
                 '原门槛保持不变，尚未启动。'))
    assert tiering.classify(spec)[:2] == ('canonical', 'specification')
    assert tiering.classify(protocol)[:2] == ('canonical', 'protocol')


def test_final_delivery_and_input_snapshot_are_not_generic_snapshots():
    delivery = row_for(
        type='Artifact', kind='artifact',
        title='metric_diagnostics snapshot_096 交付包',
        content='交付目录，逐文件核验 passed，all_packaged_size_and_hash_checks_passed=true。')
    source = row_for(
        type='Source', kind='artifact', title='raw_teacher_warmup_v4 初始状态快照',
        content='训练初始状态来源为 initial_state.npz。')
    status = row_for(type='Artifact', kind='artifact', title='status snapshot 当前状态快照',
                     content='运行状态快照。')
    assert tiering.classify(delivery)[0] == 'canonical'
    assert tiering.classify(source)[0] == 'canonical'
    assert tiering.classify(status)[:2] == ('episode', 'ephemeral_artifact')


def test_checkpoint_keeps_identity_when_its_body_cites_a_receipt():
    checkpoint = row_for(
        type='Artifact', kind='artifact', status='active',
        title='rescue/training/correction_500.pt',
        content='Terminal rescue checkpoint referenced by the receipt; SHA256 ' + 'a' * 64 + '.')
    assert tiering.classify(checkpoint)[0] == 'canonical'


def test_terminal_result_statement_can_be_knowledge_but_tolerance_pass_stays_episode():
    finding = row_for(
        title='warm 各组局部站立成功不可靠',
        content=('warm24 两种子 5/32、10/32，显著低于 teacher 的 32/32；'
                 '存在局部成功但不可靠，未显示普遍 warmup 收益。'),
        assertion_json=json.dumps({'retention': 'knowledge'}))
    tolerance = row_for(
        title='动作最大差通过 3e-5 容差',
        content='最大差 3.3e-6，通过原 atol=rtol=3e-5。')
    assert tiering.classify(finding)[0] == 'canonical'
    assert tiering.classify(tolerance)[0] == 'episode'


def test_held_out_result_is_knowledge_but_generic_test_count_is_not():
    held = row_for(
        title='最后权重在留出种子 0/64 通过',
        content=('预注册独立评估：教师留出 64/64，预先固定最后权重 0/64，'
                 '显著低于教师；新学生均 0/32。'),
        assertion_json=json.dumps({'retention': 'knowledge'}))
    count = row_for(title='16 套 CPU 契约共 146 项测试通过',
                    content='最新 16 套 CPU 契约共 146 项不同测试通过。')
    assert tiering.classify(held)[0] == 'canonical'
    assert tiering.classify(count)[0] == 'episode'


def test_quoted_fraction_is_not_treated_as_a_one_off_tally():
    """A bare 'N/M' is usually a quoted metric, not a pass/fail outcome."""
    metric = row_for(
        title='First-step and segment-transition errors dominate residual',
        content=('Per-case errors concentrate at the first step: transition 199/200 errors '
                 '~0.0093/0.1011; within-segment rmse ~0.003-0.017.'))
    tally = row_for(title='检查结果', content='本轮 4/4 通过。')
    assert tiering.classify(metric)[0] == 'canonical'
    assert tiering.classify(tally)[0] == 'episode'


def test_held_out_split_mention_without_its_own_outcome_is_not_enough():
    """Mentioning a split is not the same as reporting a result on it."""
    assert tiering._HELD_OUT_RESULT.search('留出集未使用，原 checkpoint 未变。')
    assert not tiering._HELD_OUT_EVALUATION.search('留出集未使用，原 checkpoint 未变。')
    assert tiering._HELD_OUT_EVALUATION.search('留出集评估 12/16 通过。')
    mention = row_for(
        title='PPO 接入探针运行成功但学习效果未通过',
        content='严格限速学生恢复 0/4 未通过；留出集未使用，原 checkpoint 未变。')
    assert tiering.classify(mention)[:2] == ('episode', 'single_run_result')


def test_live_rebuild_record_stays_episode_until_it_settles():
    """A number emitted by a run that is still being rebuilt is not standing."""
    live = row_for(
        title='新候选固定末端自主任务 0/8',
        content='training_admission_20261028_v1 重建中，新候选固定末端自主任务均 0/8。',
        status='active')
    settled = row_for(
        title='固定末端自主任务验收未达成',
        content='新候选固定末端自主任务 0/8，运动验收失败；未改变原判定。',
        status='completed')
    assert tiering.classify(live)[:2] == ('episode', 'live_producer_observation')
    assert tiering.classify(settled)[0] == 'canonical'


def test_measured_deviation_against_target_is_a_diagnostic_result():
    """'max difference = 0' / 'still below the requested X' are outcomes."""
    row = row_for(
        title='Teacher 0.1/0.3 指令速度跟踪诊断',
        content=('Teacher 在 0.1 指令下 7/8 轨迹基本原地站立；所有有效控制步 '
                 'obs[48:51] 与实际命令最大差值为0。0.3 指令下 8/8 存活，'
                 '运动段平均速度约 0.146–0.232 m/s，有步态但仍低于所请求的0.3。'),
        status='active')
    assert tiering.classify(row)[0] == 'canonical'


def test_purpose_reads_title_before_body():
    text = row_for(title='稳定结论：仅限固定初态的误差对照', content='报告中记录了上传地址和 sha256。')
    assert tiering._purpose(text) is None


def test_quote_only_anchor_is_reported_separately_from_full_source(env):
    store, entries = env
    publish(store, 'a', entries[0], retention='knowledge',
            assertion=[dict(subject='误差', predicate='数值', value='1', unit='none', conditions=[])])
    # Force the exact condition under test: the full original cannot be
    # recovered, while the node's own evidence quote is still present. Merely
    # blanking entries.text does not do this for externalized fixtures because
    # sg_source_text rehydrates the original from its source locator.
    store.db.create_function('sg_source_text', 2, lambda entry_id, inline: None)
    rows = list(tiering._rows(store))
    target = next(row for row in rows if row['id'] == 'a')
    assert target['anchor_scope'] == 'quote_only'
    assert target['full_source_recoverable'] is False
    assert tiering.classify(target)[0] == 'canonical'


def test_audit_separates_full_source_and_quote_only_counts(env):
    store, entries = env
    publish(store, 'a', entries[0], retention='knowledge',
            assertion=[dict(subject='误差', predicate='数值', value='1', unit='none', conditions=[])])
    report = tiering.audit(store, limit=None, max_samples=0)
    assert report['anchor_scope'] == {'full_source': 1}
    assert report['canonical_anchor_scope'] == {'full_source': 1}
    assert report['full_source_recoverable'] == {'true': 1}


def test_explicit_remember_projects_its_tier_in_the_same_write(tmp_path):
    from sessiongraph.memory import remember
    from sessiongraph.work_map import work_map
    from test_memories import make_store
    _, store, _ = make_store(tmp_path, [('assistant', '固定初态下误差 0.1，仅限该条件。')])
    tiering.setup(store)  # a project that has the v3 projection enabled
    eid = store.db.execute('SELECT id FROM entries LIMIT 1').fetchone()[0]
    mid = remember(store, kind='finding', title='固定初态误差结论',
                   content='固定初态下误差 0.1，仅限该条件。',
                   source_entry_id=eid, source_quote='固定初态下误差 0.1，仅限该条件。')['id']
    row = store.db.execute('SELECT tier,revision FROM sg_node_tiers WHERE node=?', (mid,)).fetchone()
    assert row is not None and row[0] in tiering.TIERS and row[1] == tiering.VERSION
    # No manual backfill: the new item is already visible in exactly one view.
    assert mid in {n['id'] for n in work_map(store, view='all')['nodes']}
    views = [view for view in ('knowledge', 'episode')
             if mid in {n['id'] for n in work_map(store, view=view)['nodes']}]
    assert len(views) == 1
    assert work_map(store)['tiering']['complete'] is True


def test_empty_projection_needs_no_backfill(env):
    store,_=env
    result=tiering.summary(store)
    assert result['total']==0 and result['complete'] and result['current']
