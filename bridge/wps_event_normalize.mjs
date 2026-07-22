export function normalize(data, currentBotIds, eventId) {
  const message = data?.message || {};
  const content = message.content ?? data?.content ?? data?.message ?? data;
  const type = message.type || '';
  const mentions = Array.isArray(message.mentions) && message.mentions.length
    ? message.mentions : Array.isArray(data?.mentions) ? data.mentions : [];
  let text = '';
  let attachments = [];
  let cloudDocs = [];
  let sharedDocs = [];
  if (type === 'image') {
    const image = content?.image || {};
    if (image.storage_key) attachments.push(attachment('image', image));
    text = image.name ? `[image:${image.name}]` : '[image]';
  } else if (type === 'file') {
    const file = content?.file || {};
    const subtype = file.type || (file.cloud ? 'cloud' : file.local ? 'local' : '');
    if (subtype === 'cloud' && file.cloud?.link_url) {
      cloudDocs.push({link_url: file.cloud.link_url});
      const fileId = file.cloud.id || file.cloud.link_id || '';
      if (fileId) sharedDocs.push({file_id: fileId, link_id: file.cloud.link_id || ''});
      text = '[cloud-doc]';
    } else if (subtype === 'local' && file.local?.storage_key) {
      attachments.push(attachment('file', file.local));
      text = file.local.name ? `[file:${file.local.name}]` : '[file]';
    }
  } else if (['audio', 'video'].includes(type)) {
    const media = content?.[type] || {};
    if (media.storage_key) attachments.push(attachment(type, media));
    text = `[${type}]`;
  } else if (type === 'sticker') {
    const image = content?.sticker?.image || {};
    if (image.storage_key) attachments.push(attachment('sticker', image));
    text = '[sticker]';
  } else {
    const rich = extractRichText(content, currentBotIds);
    text = content?.rich_text
      ? rich.text
      : normalizeInlineMentions(extractText(content), mentions, currentBotIds);
    attachments = rich.attachments;
    cloudDocs = rich.cloudDocs;
    sharedDocs = rich.sharedDocs;
  }
  return {
    chat_id: data?.chat?.id || data?.chat_id || '',
    chat_type: data?.chat?.type || '',
    text,
    attachments,
    cloud_docs: cloudDocs,
    shared_docs: sharedDocs,
    event_id: message.id || data?.message_id || data?.event_id || eventId || '',
    mentioned: mentionsApp(mentions, currentBotIds) || richMentionsApp(content, currentBotIds),
    sender_id: data?.sender?.id || '',
    sender_name: data?.sender?.name || data?.sender?.sender_name || '',
  };
}

function attachment(type, value) {
  return {
    type,
    storage_key: value.storage_key,
    name: value.name || '',
    size: value.size || 0,
    mime: value.type || '',
    width: value.width || 0,
    height: value.height || 0,
  };
}

function extractRichText(content, currentBotIds) {
  const result = {text: '', attachments: [], cloudDocs: [], sharedDocs: []};
  const rich = content?.rich_text;
  const rows = rich?.elements || rich?.content || [];
  if (!Array.isArray(rows)) return result;
  const parts = [];
  for (const row of rows) {
    const items = row?.elements || [row];
    for (const item of items) {
      if (!item || typeof item !== 'object') continue;
      if (item.type === 'text') parts.push(item?.text_content?.content || '');
      if (item.type === 'mention') {
        const mention = item?.mention_content || {};
        if (identityMatches(mention.identity, currentBotIds)) continue;
        if (mention.text) parts.push(`@${mention.text} `);
      }
      if (item.type === 'doc') {
        const file = item?.doc_content?.file || {};
        result.sharedDocs.push({
          file_id: file.id || '',
          link_id: file.link_id || '',
          link_url: file.link_url || '',
        });
        if (file.link_url) result.cloudDocs.push({link_url: file.link_url});
        parts.push(`[doc:${item?.doc_content?.text || ''}]`);
      }
      if (item.type === 'image' && item?.image_content?.storage_key) {
        result.attachments.push(attachment('image', item.image_content));
        parts.push('[image]');
      }
    }
  }
  result.text = parts.join('');
  return result;
}

function extractText(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(extractText).find(Boolean) || '';
  if (!value || typeof value !== 'object') return '';
  if (typeof value.content === 'string') return value.content;
  return extractText(value.text) || extractText(value.content) || Object.values(value).map(extractText).find(Boolean) || '';
}

function normalizeInlineMentions(text, mentions, currentBotIds) {
  if (!text || !text.includes('<at')) return text || '';
  const byId = new Map(
    (Array.isArray(mentions) ? mentions : [])
      .filter(mention => mention?.id != null)
      .map(mention => [String(mention.id), mention]),
  );
  return text.replace(
    /<at\s+id="([^"]+)"\s*>([^<]*)<\/at>\s*/gi,
    (_match, id, label) => {
      const mention = byId.get(String(id));
      const identity = mention?.identity;
      if (identityMatches(identity, currentBotIds)) return '';
      const name = identity?.name || label || '';
      return name ? `@${name} ` : '';
    },
  );
}

function mentionsApp(mentions, app) {
  return Array.isArray(mentions) && mentions.some(item => identityMatches(item?.identity, app));
}
function richMentionsApp(content, app) {
  const rows = content?.rich_text?.elements || content?.rich_text?.content || [];
  return Array.isArray(rows) && rows.some(row => (row?.elements || [row]).some(item => item?.type === 'mention' && identityMatches(item?.mention_content?.identity, app)));
}
function identityMatches(identity, botIds) {
  if (!identity || !['sp', 'app'].includes(identity.type)) return false;
  const ids = Array.isArray(botIds) ? botIds : [botIds];
  return [identity.app_id, identity.id].some(id => id && ids.includes(id));
}
