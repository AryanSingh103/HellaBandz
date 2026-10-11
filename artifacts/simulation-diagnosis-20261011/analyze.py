import json
import math
from collections import Counter, defaultdict
from pathlib import Path
import numpy as np

source = Path(r'C:\Users\Krish\Downloads\simulation-sim-mv3k44dq.json')
d = json.loads(source.read_text())

def metrics(values):
    e = np.array(values, dtype=float)
    r = np.diff(e) / e[:-1]
    return {'return_pct': (e[-1] / e[0] - 1) * 100,
            'sharpe_hourly_252x7': float(r.mean() / r.std() * math.sqrt(252*7)) if r.std() else 0,
            'drawdown_pct': float(((np.maximum.accumulate(e)-e)/np.maximum.accumulate(e)).max()*100),
            'first_equity': float(e[0]), 'final_equity': float(e[-1]), 'observations': len(e)}

baseline = d['buy_and_hold_comparison']['baseline']
out = {'source': str(source), 'dataset': d['simulation']['dataset'],
       'pipeline_kind': d['simulation']['config']['kind'],
       'portfolio': metrics([s['account_value'] for s in d['steps']]),
       'baseline': metrics([x['equity']/1e6 for x in baseline]),
       'agents': {}, 'rejections': Counter(), 'thinking_kinds': Counter(x['kind'] for x in d['thinking'])}
for x in d['decisions']:
    if x['type'] == 'OrderRejected':
        out['rejections'].update(x['risk_rules'])
for a in d['agents']:
    agent = a['agent']
    fills = [x for x in d['trades'] if x['agent']==agent]
    holds = defaultdict(list)
    for x in fills:
        holds[x['symbol']].append(x)
    pairs = []
    for sym, trades in holds.items():
        for prev, cur in zip(trades, trades[1:]):
            if prev['side'] != cur['side']:
                pairs.append(cur['step']-prev['step'])
    out['agents'][agent] = {'family': a['rule_type'], 'params': a['rule_settings'],
        'pnl': a['profit_or_loss_at_end'], 'fills': len(fills),
        'fees': sum(x['fee'] for x in fills), 'modeled_slippage': sum(x['slippage'] for x in fills),
        'traded_notional': sum(x['qty']*x['price'] for x in fills),
        'consecutive_opposite_side_gaps_bars': dict(Counter(pairs)),
        'validation': a['judged_on_unseen_data']['validation'],
        'training': a['judged_on_unseen_data']['training']}
by_step_sym = defaultdict(list)
for x in d['trades']:
    by_step_sym[x['step'],x['symbol']].append(x)
conflicts=[]
for (step,sym),fills in by_step_sym.items():
    if any(a['agent']!=b['agent'] and a['side']!=b['side'] for a in fills for b in fills):
        conflicts.append({'step':step,'symbol':sym,'trades':fills})
out['cross_agent_opposing_fill_groups'] = len(conflicts)
out['cross_agent_opposing_fill_examples'] = conflicts[:3]
out['mean_exposure_pct'] = float(np.mean([s['invested_value']/s['account_value'] for s in d['steps']])*100)
out['model_counts'] = dict(Counter(x.get('llm',{}).get('model','none') for x in d['thinking'] if x['kind']=='ai_remark'))
out['symbol_returns_pct'] = {sym:(d['steps'][-1]['closes'][sym]/px-1)*100 for sym,px in d['steps'][0]['closes'].items()}
out['pnl_reconciliation_residual_usd'] = d['metrics']['account_value']-10000-sum(a['profit_or_loss_at_end'] for a in d['agents'])
out['relative_return_pp'] = out['portfolio']['return_pct']-out['baseline']['return_pct']
out['evidence_limitations'] = ['Synthetic seed42 data, not live or real historical stock prices.',
    'Legacy session and previously frozen research manifest, not coordinated registry.',
    'Reported slippage is embedded in fill prices; adding it back is diagnostic, not a frictionless replay.',
    'Opposing fills at the same step demonstrate unnetted opposing executions, not exchange self-trading.']
target=Path(__file__).with_name('analysis.json')
target.write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps(out,indent=2))
