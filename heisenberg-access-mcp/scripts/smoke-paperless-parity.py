"""Read-only Paperless parity checks, executed inside one configured MCP container."""
import argparse
import asyncio
import json
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

PAPERLESS_TOOLS = {
    'paperless.search_documents', 'paperless.get_document', 'paperless.read_document',
    'paperless.list_metadata', 'paperless.update_document', 'paperless.delete_document',
    'paperless.create_correspondent', 'paperless.create_document_type',
    'paperless.bulk_set_document_type',
}


async def main(source):
    token_name, expected_count, wrong_source = {
        'private': ('HEISENBERG_ACCESS_MCP_TOKEN', 35, 'work'),
        'work': ('HEISENBERG_WORK_MCP_TOKEN', 10, 'private'),
    }[source]
    base = 'http://127.0.0.1:8000'
    report = {'source': source}
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as plain:
        assert (await plain.get(base + '/health')).status_code == 200
        for headers in ({}, {'Authorization': 'Bearer invalid-parity-test'}):
            assert (await plain.get(base + '/mcp', headers=headers)).status_code == 401
    report['health_and_auth_boundary'] = True
    async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + os.environ[token_name]}, timeout=60, follow_redirects=False) as client:
        async with streamable_http_client(base + '/mcp', http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert len(tools) == expected_count
                paperless = {t.name: t for t in tools if t.name.startswith('paperless.')}
                assert set(paperless) == PAPERLESS_TOOLS
                assert paperless['paperless.delete_document'].annotations.destructiveHint is True
                assert paperless['paperless.create_correspondent'].annotations.destructiveHint is False
                for name in ('paperless.update_document', 'paperless.delete_document', 'paperless.create_correspondent', 'paperless.create_document_type', 'paperless.bulk_set_document_type'):
                    assert paperless[name].annotations.readOnlyHint is False
                report['total_tools'] = len(tools)
                report['paperless_tools'] = sorted(paperless)

                async def call(name, args):
                    result = await session.call_tool(name, args)
                    assert not result.isError, name + ' returned MCP error'
                    data = result.structuredContent
                    if data is None:
                        data = json.loads(next(c.text for c in result.content if c.type == 'text'))
                    return data

                status = await call('access_status', {})
                assert status.get('ok') and status['source'] == source
                assert PAPERLESS_TOOLS <= set(status['capabilities'])
                search = await call('paperless.search_documents', {'query': 'Rechnung', 'page_size': 10})
                assert search.get('ok') and search['documents']
                ref = next(d['ref'] for d in search['documents'] if d['ref'] not in {'work:1945', 'work:1950'})
                assert ref.startswith(source + ':')
                before = await call('paperless.get_document', {'document_ref': ref})
                assert before.get('ok')
                text = await call('paperless.read_document', {'document_ref': ref, 'limit': 32})
                assert text.get('ok') and len(text.get('text', '')) <= 32
                for kind in ('tags', 'correspondents', 'document_types'):
                    meta = await call('paperless.list_metadata', {'kind': kind, 'page_size': 1})
                    assert meta.get('ok')
                    if kind == 'document_types':
                        document_type_count = meta['count']
                        assert meta['items'], 'An existing type is needed for the read-only bulk preview'
                        existing_type = meta['items'][0]
                        type_id = existing_type['id']
                        type_duplicate = await call('paperless.create_document_type', {'name': existing_type['name'], 'dry_run': True})
                        assert type_duplicate.get('ok') and type_duplicate['duplicate'] and not type_duplicate['created']
                    if kind == 'correspondents':
                        correspondent_count = meta['count']
                        if meta['items']:
                            existing = await call('paperless.create_correspondent', {'name': meta['items'][0]['name'], 'dry_run': True})
                            assert existing.get('duplicate') or existing.get('error') == 'paperless_correspondent_duplicate_lookup_ambiguous'
                update = await call('paperless.update_document', {'document_ref': ref, 'changes': {'title': before['title']}, 'dry_run': True})
                assert update.get('ok') and update['dry_run']
                delete = await call('paperless.delete_document', {'document_ref': ref, 'dry_run': True})
                assert delete.get('ok') and delete['dry_run'] and delete['would_move_to_trash']
                create = await call('paperless.create_correspondent', {'name': 'Heisenberg – nur Vorschau zur Tool-Parität', 'dry_run': True})
                assert create.get('ok') and create['dry_run'] and create['would_create'] and not create['created']
                type_create = await call('paperless.create_document_type', {'name': 'Heisenberg – nur Vorschau zur Tool-Parität', 'dry_run': True})
                assert type_create.get('ok') and type_create['dry_run'] and type_create['would_create'] and not type_create['created']
                bulk = await call('paperless.bulk_set_document_type', {'document_refs': [ref], 'document_type_id': type_id, 'dry_run': True})
                assert bulk.get('ok') and bulk['dry_run']
                for name, arguments, error in (
                    ('paperless.update_document', {'document_ref': ref, 'changes': {'title': before['title']}}, 'paperless_update_confirmation_required'),
                    ('paperless.delete_document', {'document_ref': ref}, 'paperless_delete_confirmation_required'),
                    ('paperless.create_correspondent', {'name': 'Preview'}, 'paperless_create_correspondent_confirmation_required'),
                    ('paperless.create_document_type', {'name': 'Preview'}, 'paperless_create_document_type_confirmation_required'),
                    ('paperless.bulk_set_document_type', {'document_refs': [ref], 'document_type_id': type_id}, 'paperless_bulk_set_document_type_confirmation_required'),
                    ('paperless.bulk_set_document_type', {'document_refs': [wrong_source + ':1'], 'document_type_id': type_id, 'dry_run': True}, 'paperless_document_ref_invalid'),
                    ('paperless.delete_document', {'document_ref': wrong_source + ':1', 'dry_run': True}, 'paperless_document_ref_invalid'),
                    ('paperless.get_document', {'document_ref': wrong_source + ':1'}, 'paperless_document_ref_invalid'),
                ):
                    denied = await call(name, arguments)
                    assert denied.get('ok') is False and denied.get('error') == error
                after = await call('paperless.get_document', {'document_ref': ref})
                assert before == after
                metadata_after = await call('paperless.list_metadata', {'kind': 'correspondents', 'page_size': 1})
                assert metadata_after.get('ok') and metadata_after['count'] == correspondent_count
                types_after = await call('paperless.list_metadata', {'kind': 'document_types', 'page_size': 1})
                assert types_after.get('ok') and types_after['count'] == document_type_count
                report.update(reads=True, previews=True, confirmations=True, source_boundary=True, document_unchanged=True, correspondent_count_unchanged=True, document_type_count_unchanged=True, provider_write_calls=0)
    print(json.dumps(report, indent=2))


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', required=True, choices=('private', 'work'))
asyncio.run(main(parser.parse_args().source))
