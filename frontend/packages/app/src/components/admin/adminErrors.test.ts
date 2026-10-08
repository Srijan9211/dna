import { describe, expect, it } from 'vitest';
import {
  describeAdminError,
  parseJsonObject,
  withoutMaskedSecrets,
} from './adminErrors';

const http = (status: number, detail: unknown) => ({
  response: { status, data: { detail } },
});

describe('describeAdminError', () => {
  it('shows a plain string detail', () => {
    expect(describeAdminError(http(404, 'No config revision 9')).message).toBe(
      'No config revision 9'
    );
  });

  it('lists every issue of a rejected change (422)', () => {
    const issues = [
      {
        section: 'llm',
        path: 'llm.provider',
        message: 'bad',
        source: 'runtime',
      },
    ];
    const e = describeAdminError(
      http(422, { message: 'Invalid configuration', issues })
    );
    expect(e.message).toBe('Invalid configuration');
    expect(e.issues).toEqual(issues);
    expect(e.stale).toBe(false);
  });

  it('flags a stale save so the UI can offer a reload (409 with a string)', () => {
    const e = describeAdminError(
      http(409, 'expected revision 1, store is at 2')
    );
    expect(e.stale).toBe(true);
    expect(e.message).toContain('store is at 2');
  });

  it('carries the report when a failing handshake refused to save (409 with a report)', () => {
    const report = {
      ok: false,
      checks: [],
      suggestions: [],
      connector: 'ayon',
    };
    const e = describeAdminError(
      http(409, { message: 'nothing was saved', report })
    );
    expect(e.message).toBe('nothing was saved');
    expect(e.report).toEqual(report);
    expect(e.stale).toBe(false);
  });

  it('explains an authorisation failure', () => {
    expect(
      describeAdminError(
        http(403, 'You are not a configuration administrator.')
      ).message
    ).toContain('not a configuration administrator');
    expect(describeAdminError(http(401, undefined)).message).toMatch(
      /not allowed/
    );
  });

  it('explains an unreachable server', () => {
    expect(describeAdminError({ message: 'Network Error' }).message).toBe(
      'Could not reach the server: Network Error'
    );
  });

  it('never throws on junk', () => {
    for (const junk of [null, undefined, 42, 'x', [], {}]) {
      expect(describeAdminError(junk).message).toBeTruthy();
    }
  });
});

describe('parseJsonObject', () => {
  it('parses an object', () => {
    expect(parseJsonObject('{"a": {"b": 1}}')).toEqual({
      ok: true,
      value: { a: { b: 1 } },
    });
  });

  it('rejects invalid JSON and non-objects with a reason', () => {
    expect(parseJsonObject('{nope')).toMatchObject({
      ok: false,
      error: expect.stringContaining('Not valid JSON'),
    });
    for (const text of ['[1]', '"s"', '3', 'null']) {
      expect(parseJsonObject(text)).toEqual({
        ok: false,
        error: expect.stringContaining('JSON object'),
      });
    }
  });
});

describe('withoutMaskedSecrets', () => {
  it('removes the masked placeholder wherever it is, keeping everything else', () => {
    expect(
      withoutMaskedSecrets({
        url: 'http://x',
        api_key: '********',
        nested: { token: '********', keep: 1 },
        list: [1, 2],
      })
    ).toEqual({ url: 'http://x', nested: { keep: 1 }, list: [1, 2] });
  });

  it('leaves a real reference alone', () => {
    expect(withoutMaskedSecrets({ api_key: '${env:K}' })).toEqual({
      api_key: '${env:K}',
    });
  });
});
