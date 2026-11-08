"""Compile independent browser journeys with one observable assertion per checkpoint."""
import json


class MissingAssertion(ValueError):
    """The suite lacks assertion evidence; changing application code cannot supply it."""


MATCHERS = frozenset("""
    toBeAttached toBeChecked toBeDisabled toBeEditable toBeEmpty toBeEnabled toBeFocused
    toBeHidden toBeInViewport toBeVisible toContainText toHaveAccessibleDescription
    toHaveAccessibleName toHaveAttribute toHaveClass toHaveCount toHaveCSS toHaveId
    toHaveJSProperty toHaveRole toHaveText toHaveTitle toHaveURL toHaveValue toHaveValues
""".split())

AUTHOR_RULES = """Design independent browser journeys from the original requirements.
Return {helpers?:string,cases:[{title:string,steps:[{id:string,body?:string,
target:string,matcher:string,args?:array,not?:boolean}]}]}. Each checkpoint has ONE
original coverage ID and a required browser assertion. Repeat an ID for multiple outcomes.
body is optional async TypeScript setup/actions with page; target is a separate TypeScript
expression returning a Playwright Locator or Page using page or shared helpers.
args is a JSON array of literal matcher arguments, default []. Example checkpoint:
{"id":"STEP-1","body":"await page.goto('/');","target":"page.getByRole('button',{name:'Delete',exact:true})","matcher":"toBeVisible"}.
Allowed matchers: """ + ", ".join(sorted(MATCHERS)) + """.
helpers may define shared functions using the Page type. Cover every original ID.
Use real UI actions including prerequisites. No application imports, database/API setup,
network mocks, arbitrary sleeps, skipped tests or conditional assertions.
Treat explicit role, accessible name, route, data identity and persistence as requirements.
Use exact roles/names when specified. Check the original record after cancel; reload before
checking persistence. Check observable outcomes including absence when specified.
Images inform appearance. Keep shared setup short."""


def render_suite(response, coverage):
    if not isinstance(response, dict) or not isinstance(response.get('cases'), list):
        raise ValueError('Return cases as a JSON array')
    helpers = response.get('helpers', '')
    if not isinstance(helpers, str) or not response['cases']:
        raise ValueError('Provide a nonempty case list and optional TypeScript helpers')
    source = ["import {test, expect} from '../support/context';",
              "import type {Page, Locator} from '@playwright/test';", helpers]
    required = set(coverage)
    covered = set()
    for case in response['cases']:
        if not isinstance(case, dict) or not isinstance(case.get('title'), str) or not case['title'].strip():
            raise ValueError('Each case needs a title')
        title = case['title']
        steps = case.get('steps')
        if not isinstance(steps, list) or not steps:
            raise MissingAssertion(f'{title}: provide assertion checkpoints for claimed coverage')
        ids = []
        blocks = []
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get('id'), str) or step['id'] not in required:
                raise ValueError(f'{title}: use original coverage IDs')
            step_id = step['id']
            target, matcher = step.get('target'), step.get('matcher')
            if (not isinstance(target, str) or not target.strip()
                    or not isinstance(matcher, str) or matcher not in MATCHERS):
                raise MissingAssertion(f'{step_id}: provide a browser target and supported matcher')
            args, body, negate = step.get('args', []), step.get('body', ''), step.get('not', False)
            if not isinstance(args, list) or not isinstance(body, str) or not isinstance(negate, bool):
                raise ValueError(f'{step_id}: invalid matcher arguments, actions or negation')
            arguments = ', '.join(json.dumps(arg, allow_nan=False) for arg in args)
            label = json.dumps(step_id)
            # Action-local control flow cannot bypass the host-owned assertion or its receipt.
            actions = f'    await (async () => {{\n{body}\n    }})();\n' if body.strip() else ''
            blocks.append(
                f'  await test.step({label}, async () => {{\n{actions}'
                f'    const subject = (\n{target}\n    ) satisfies Page | Locator;\n'
                f'    await expect(subject, {label}).{"not." if negate else ""}{matcher}({arguments});\n'
                f'    info.annotations.push({{type:"arc-asserted", description:{label}}});\n  }});')
            ids.append(step_id)
        covered.update(ids)
        annotations = [{'type': 'arc-step', 'description': i} for i in dict.fromkeys(ids)]
        source.append(f'test({json.dumps(title)}, {{annotation:{json.dumps(annotations)}}}, async ({{page}}, info) => {{')
        source.extend(blocks)
        source.append('});')
    if missing := required - covered:
        raise ValueError('Missing required outcomes: ' + ', '.join(sorted(missing)))
    return '\n'.join(source) + '\n'
