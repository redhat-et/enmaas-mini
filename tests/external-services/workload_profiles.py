"""Synthetic display identities and client metadata shared by local/RHEL tests.

Names and directory locations are fictional fixtures. User-Agent values simulate
client categories; this runner does not launch those products or spoof source IPs.
"""
PEOPLE = (
    ('Maya Chen', 'US', 'CA', 'Platform'),
    ('Oliver Novak', 'CZ', '', 'Platform'),
    ('Amara Okafor', 'GB', '', 'Product'),
    ('Lucas Silva', 'BR', '', 'Product'),
    ('Yuki Tanaka', 'JP', '', 'Research'),
    ('Priya Rao', 'IN', '', 'Research'),
    ('Sofia Garcia', 'ES', '', 'Support'),
    ('Noah Martin', 'CA', 'ON', 'Support'),
    ('Elena Rossi', 'IT', '', 'Product'),
    ('Daniel Weber', 'DE', '', 'Platform'),
    ('Fatima Hassan', 'GB', '', 'Research'),
    ('Liam Wilson', 'AU', '', 'Support'),
    ('Isabella Costa', 'BR', '', 'Product'),
    ('Arjun Patel', 'IN', '', 'Platform'),
    ('Hana Sato', 'JP', '', 'Research'),
    ('Release Review Bot', 'DE', '', 'Automation'),
    ('Nightly Evaluation Bot', 'US', 'VA', 'Automation'),
    ('Security Review Bot', 'GB', '', 'Automation'),
    ('Documentation Bot', 'CZ', '', 'Automation'),
    ('Dependency Update Bot', 'DE', '', 'Automation'),
    ('Test Generation Bot', 'IN', '', 'Automation'),
    ('Incident Triage Bot', 'US', 'CA', 'Automation'),
    ('Translation Bot', 'JP', '', 'Automation'),
    ('Support Summary Bot', 'CA', 'ON', 'Automation'),
    ('Release Notes Bot', 'ES', '', 'Automation'),
)
CLIENTS = ('opencode/1.0 enmaas-synthetic', 'claude-code/2.1.0 enmaas-synthetic',
           'OpenAI/Python 1.0 enmaas-synthetic', 'OpenAI/JS 1.0 enmaas-synthetic',
           'codex/1.0 enmaas-synthetic')


def persona(index):
    if type(index) is not int or not 0 <= index < 2500:
        raise ValueError('Synthetic person index must be 0–2499')
    name, country, state, team = PEOPLE[index % len(PEOPLE)]
    if index >= len(PEOPLE):
        name += ' ' + str(index // len(PEOPLE) + 1)
    return {'name': name, 'country': country, 'state': state, 'team': team,
            'account_type': 'bot' if ' Bot' in name else 'person',
            'monthly_usd': 1000 if ' Bot' in name else 300,
            'user_agent': CLIENTS[index % len(CLIENTS)]}


def client_for_api(index, api):
    persona(index)  # Common index validation.
    if api == 'anthropic': return 'claude-code/2.1.0 enmaas-synthetic'
    if api != 'openai': raise ValueError('Unsupported synthetic client API')
    clients = (CLIENTS[0], CLIENTS[2], CLIENTS[3], CLIENTS[4])
    return clients[index % len(clients)]


def directory_tags(profile, subject):
    first, last = profile['name'].split(' ', 1)
    return {'email':subject, 'first_name':first, 'last_name':last,
            'country':profile['country'], 'state':profile['state'],
            'department':profile['team'], 'cost_center':profile['team'].lower(),
            'cost_center_description':profile['team']+' (synthetic)',
            'employee_type':'service_account' if profile['account_type']=='bot' else 'synthetic_person'}


def workload(index):
    """Reproducible range across users/jobs; simulated counts, not tokenized text."""
    profile=persona(index)
    choices=((512,128,150,60,'Explain a small function and suggest a focused test.'),
             (2048,384,350,100,'Review a pull request for correctness and security.'),
             (8192,1024,700,180,'Summarize repository context and propose a migration plan.'),
             (4096,768,500,140,'Investigate a failing build and propose a patch.'))
    inp,out,ttft,chunk,task=choices[index % len(choices)]
    if profile['account_type']=='bot':
        inp,out,ttft,chunk,task=(
            (16384,1536,900,220,'Evaluate nightly tests and summarize release risks.'),
            (4096,512,500,120,'Summarize a dependency update and its test results.'),
            (8192,1024,700,180,'Review security findings and propose focused fixes.')
            )[index % 3]
    return {'input_tokens':inp,'output_tokens':out,'ttft_ms':ttft,'chunk_ms':chunk,'task':task}


def budget_profile(index, *, mixed=False):
    """Opt-in dashboard scenario; capacity runs retain their normal allowances."""
    profile=persona(index)
    if mixed:
        slot=index % len(PEOPLE)
        profile=dict(profile, monthly_usd=(.01 if slot in (1,4,15,18) else
                     (150,500,1000)[index % 3] if profile['account_type']=='bot' else
                     (25,100,300)[index % 3]))
    return profile
