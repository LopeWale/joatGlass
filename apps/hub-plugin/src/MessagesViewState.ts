export type MessagesScreen =
  | 'conversations'
  | 'conversation'
  | 'review'
  | 'compose'
  | 'access'
  | 'setup'
  | 'empty'
  | 'read_only'
  | 'retry'
  | 'invalid';

export type MessagesRowAction = 'select_conversation' | 'reply' | 'review' | 'retry' | 'none';

export interface MessagesViewState {
  screen: MessagesScreen;
  rowAction: MessagesRowAction;
  selectable: boolean;
}

export function isCommsTitle(title?: string): boolean {
  const normalized = title?.trim().toUpperCase();
  return normalized === 'MESSAGES' || normalized === 'MAIL';
}

/** Convert the brain-authored generic wire title into a closed local state set. */
export function resolveMessagesViewState(
  title: string,
  wireSelectable: boolean,
): MessagesViewState {
  const normalized = title.trim().toUpperCase();
  const family = normalized === 'MESSAGES' || normalized.startsWith('MESSAGES · ')
    ? 'MESSAGES'
    : normalized === 'MAIL' || normalized.startsWith('MAIL · ')
      ? 'MAIL'
      : undefined;
  if (!family) {
    return { screen: 'invalid', rowAction: 'none', selectable: false };
  }
  if (normalized === family) {
    return {
      screen: 'conversations',
      rowAction: wireSelectable ? 'select_conversation' : 'none',
      selectable: wireSelectable,
    };
  }
  const suffix = normalized.slice(family.length + 3);
  if (suffix === 'CONVERSATION') {
    return {
      screen: 'conversation',
      rowAction: wireSelectable ? 'reply' : 'none',
      selectable: wireSelectable,
    };
  }
  if (suffix === 'REPLY') {
    return { screen: 'conversation', rowAction: 'none', selectable: false };
  }
  if (suffix === 'REVIEW') {
    return { screen: 'review', rowAction: 'review', selectable: wireSelectable };
  }
  if (suffix === 'COMPOSE') {
    return { screen: 'compose', rowAction: 'none', selectable: false };
  }
  if (suffix === 'ACCESS') {
    return { screen: 'access', rowAction: 'retry', selectable: true };
  }
  if (suffix === 'SETUP') {
    return { screen: 'setup', rowAction: 'retry', selectable: true };
  }
  if (suffix === 'RETRY') {
    return { screen: 'retry', rowAction: 'retry', selectable: true };
  }
  if (suffix === 'EMPTY') {
    return { screen: 'empty', rowAction: 'none', selectable: false };
  }
  if (suffix === 'READ ONLY') {
    return { screen: 'read_only', rowAction: 'none', selectable: false };
  }
  return { screen: 'invalid', rowAction: 'none', selectable: false };
}
