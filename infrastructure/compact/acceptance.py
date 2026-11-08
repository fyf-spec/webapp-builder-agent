"""Compile independently authored browser journeys into the fixed Playwright suite."""
import json


AUTHOR_RULES = """Design independent browser journeys from the original requirements.
Return {helpers?:string,cases:[{title:string,covers:string[],body:string}]}.
body is the TypeScript body of an async Playwright test with page and expect available.
helpers may define shared functions using the Page type. The host supplies imports, tests
and coverage annotations. Cover every supplied ID. Combine related outcomes in a journey.
Use real UI actions, including prerequisites. No application imports, database/API setup,
network mocks, arbitrary sleeps, skipped tests or conditional assertions.
Treat explicit role, accessible name, route, data identity and persistence as requirements.
For a required named button, use getByRole('button',{name:...,exact:true}); allow alternate
roles only when the original text allows them. Verify state after cancel/reload when specified.
Check observable outcomes, including absence when specified. Images inform appearance.
Keep shared setup short; do not repeat the original requirements in comments or titles."""


def render_suite(response, coverage):
    if not isinstance(response, dict) or not isinstance(response.get('cases'), list):
        raise ValueError('Return cases as a JSON array')
    helpers = response.get('helpers', '')
    if not isinstance(helpers, str) or not response['cases']:
        raise ValueError('Provide a nonempty case list and optional TypeScript helpers')
    source = ["import {test, expect} from '../support/context';",
              "import type {Page} from '@playwright/test';", helpers]
    required = set(coverage)
    covered = set()
    for case in response['cases']:
        if not isinstance(case, dict):
            raise ValueError('Each case needs title, covers, and body')
        title, ids, body = (case.get(key) for key in ('title', 'covers', 'body'))
        if not (isinstance(title, str) and title.strip() and isinstance(body, str) and body.strip()):
            raise ValueError('Each case needs a nonempty title and body')
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in required for i in ids):
            raise ValueError(f'{title}: covers must contain original coverage IDs')
        covered.update(ids)
        annotations = [{'type': 'arc-step', 'description': i} for i in dict.fromkeys(ids)]
        source.append(f'test({json.dumps(title)}, {{annotation:{json.dumps(annotations)}}}, async ({{page}}) => {{\n{body}\n}});')
    if missing := required - covered:
        raise ValueError('Missing required outcomes: ' + ', '.join(sorted(missing)))
    return '\n'.join(source) + '\n'
