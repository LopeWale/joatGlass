import { describe, expect, it } from 'vitest';
import { isCommsTitle, resolveMessagesViewState } from './MessagesViewState';

describe('MessagesViewState', () => {
  it('identifies MESSAGES and MAIL as shared comms titles', () => {
    expect(isCommsTitle('MESSAGES')).toBe(true);
    expect(isCommsTitle('MAIL')).toBe(true);
    expect(isCommsTitle('MESSAGES · CONVERSATION')).toBe(false);
    expect(isCommsTitle('MAIL · CONVERSATION')).toBe(false);
    expect(isCommsTitle('REMINDERS')).toBe(false);
    expect(isCommsTitle(undefined)).toBe(false);
  });

  it.each(['MESSAGES', 'MAIL'] as const)('allows row selection only on an authorized %s conversation list', (family) => {
    expect(resolveMessagesViewState(family, true)).toEqual({
      screen: 'conversations', rowAction: 'select_conversation', selectable: true,
    });
    expect(resolveMessagesViewState(family, false)).toEqual({
      screen: 'conversations', rowAction: 'none', selectable: false,
    });
  });

  it.each([
    ['MESSAGES · ACCESS', 'access'],
    ['MESSAGES · SETUP', 'setup'],
    ['MESSAGES · RETRY', 'retry'],
    ['MAIL · ACCESS', 'access'],
    ['MAIL · SETUP', 'setup'],
    ['MAIL · RETRY', 'retry'],
  ])('gives %s one truthful retry action', (title, screen) => {
    expect(resolveMessagesViewState(title, false)).toEqual({
      screen, rowAction: 'retry', selectable: true,
    });
  });

  it.each(['MESSAGES', 'MAIL'] as const)('lets a tap on an authorized %s thread reply to it', (family) => {
    expect(resolveMessagesViewState(`${family} · CONVERSATION`, true)).toEqual({
      screen: 'conversation', rowAction: 'reply', selectable: true,
    });
    expect(resolveMessagesViewState(`${family} · CONVERSATION`, false)).toEqual({
      screen: 'conversation', rowAction: 'none', selectable: false,
    });
    expect(resolveMessagesViewState(`${family} · REVIEW`, true)).toEqual({
      screen: 'review', rowAction: 'review', selectable: true,
    });
  });

  it.each([
    ['MESSAGES · REPLY', 'conversation'],
    ['MESSAGES · COMPOSE', 'compose'],
    ['MESSAGES · EMPTY', 'empty'],
    ['MESSAGES · READ ONLY', 'read_only'],
    ['MAIL · REPLY', 'conversation'],
    ['MAIL · COMPOSE', 'compose'],
    ['MAIL · EMPTY', 'empty'],
    ['MAIL · READ ONLY', 'read_only'],
  ])('keeps %s informational', (title, screen) => {
    expect(resolveMessagesViewState(title, true)).toEqual({
      screen, rowAction: 'none', selectable: false,
    });
  });

  it.each(['MESSAGES · FORGED', 'MAIL · FORGED'])('fails closed for an unknown title: %s', (title) => {
    expect(resolveMessagesViewState(title, true)).toEqual({
      screen: 'invalid', rowAction: 'none', selectable: false,
    });
  });

  it('fails closed for the retired single-message reader title', () => {
    expect(resolveMessagesViewState('MESSAGES · MESSAGE', true)).toEqual({
      screen: 'invalid', rowAction: 'none', selectable: false,
    });
  });
});
