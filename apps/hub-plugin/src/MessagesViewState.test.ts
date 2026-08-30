import { describe, expect, it } from 'vitest';
import { resolveMessagesViewState } from './MessagesViewState';

describe('MessagesViewState', () => {
  it('allows row selection only on a Mac-authorized conversation list', () => {
    expect(resolveMessagesViewState('MESSAGES', true)).toEqual({
      screen: 'conversations', rowAction: 'select_conversation', selectable: true,
    });
    expect(resolveMessagesViewState('MESSAGES', false)).toEqual({
      screen: 'conversations', rowAction: 'none', selectable: false,
    });
  });

  it.each([
    ['MESSAGES · ACCESS', 'access'],
    ['MESSAGES · SETUP', 'setup'],
    ['MESSAGES · RETRY', 'retry'],
  ])('gives %s one truthful retry action', (title, screen) => {
    expect(resolveMessagesViewState(title, false)).toEqual({
      screen, rowAction: 'retry', selectable: true,
    });
  });

  it.each([
    ['MESSAGES · CONVERSATION', 'conversation'],
    ['MESSAGES · REPLY', 'conversation'],
    ['MESSAGES · COMPOSE', 'compose'],
    ['MESSAGES · EMPTY', 'empty'],
    ['MESSAGES · READ ONLY', 'read_only'],
  ])('keeps %s informational', (title, screen) => {
    expect(resolveMessagesViewState(title, true)).toEqual({
      screen, rowAction: 'none', selectable: false,
    });
  });

  it('fails closed for an unknown Messages title', () => {
    expect(resolveMessagesViewState('MESSAGES · FORGED', true)).toEqual({
      screen: 'invalid', rowAction: 'none', selectable: false,
    });
  });
});
