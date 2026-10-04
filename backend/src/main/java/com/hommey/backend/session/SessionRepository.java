package com.hommey.backend.session;

import com.hommey.backend.common.*;
import java.util.*;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

@Repository
public class SessionRepository {
  private final JdbcClient jdbc;
  private final JsonCodec json;

  public SessionRepository(JdbcClient jdbc, JsonCodec json) {
    this.jdbc = jdbc;
    this.json = json;
  }

  public void requireSession(String user, String session) {
    if (jdbc.sql(
                "SELECT COUNT(*) FROM conversation_sessions WHERE user_id=:user AND session_id=:session AND close_reason IS DISTINCT FROM 'deleted' AND close_reason IS DISTINCT FROM 'cleared'")
            .param("user", user)
            .param("session", UUID.fromString(session))
            .query(Integer.class)
            .single()
        == 0) throw new ApiException(404, "SESSION_NOT_FOUND", "会话不存在或已被删除");
  }

  public List<Map<String, Object>> sessions(String user) {
    return jdbc
        .sql(
            """
            SELECT s.session_id,COALESCE(t.title,(SELECT LEFT(content,36) FROM conversation_messages m
              WHERE m.session_id=s.session_id AND m.role='user' AND m.deleted_at IS NULL AND m.retention_until>NOW()
              ORDER BY sequence_no LIMIT 1),'新会话') AS title,s.started_at AS created_at,s.last_active_at AS updated_at,
              (SELECT COUNT(*) FROM conversation_messages m WHERE m.session_id=s.session_id AND m.deleted_at IS NULL AND m.retention_until>NOW()) AS message_count
            FROM conversation_sessions s LEFT JOIN chat_session_titles t ON t.user_id=s.user_id AND t.session_id=s.session_id::text
            WHERE s.user_id=:user AND s.close_reason IS DISTINCT FROM 'deleted' AND s.close_reason IS DISTINCT FROM 'cleared'
            ORDER BY s.last_active_at DESC LIMIT 100
            """)
        .param("user", user)
        .query()
        .listOfRows()
        .stream()
        .map(this::decode)
        .toList();
  }

  public String create(String user) {
    String id = UUID.randomUUID().toString();
    jdbc.sql(
            "INSERT INTO conversation_sessions(session_id,user_id,status) VALUES(:id,:user,'active')")
        .param("id", UUID.fromString(id))
        .param("user", user)
        .update();
    return id;
  }

  public List<String> allSessionIds(String user) {
    return jdbc.sql(
            "SELECT session_id::text FROM conversation_sessions WHERE user_id=:user AND close_reason IS DISTINCT FROM 'deleted' AND close_reason IS DISTINCT FROM 'cleared'")
        .param("user", user)
        .query(String.class)
        .list();
  }

  public String title(String user, String session) {
    return jdbc.sql(
            "SELECT title FROM chat_session_titles WHERE user_id=:user AND session_id=:session")
        .param("user", user)
        .param("session", session)
        .query(String.class)
        .optional()
        .orElse("新会话");
  }

  public List<Map<String, Object>> messages(
      String user, String session, int limit, String excludeRequest) {
    if (session != null) requireSession(user, session);
    return jdbc
        .sql(
            """
            SELECT m.message_id,m.request_id,m.turn_id,m.session_id,m.user_id,m.sequence_no,m.role,m.content,m.content_type,
              m.answer_document,m.presentation_document,m.created_at,
              COALESCE((SELECT jsonb_agg(jsonb_build_object('id',a.id,'filename',a.filename,'kind',a.kind) ORDER BY link.created_at)
                FROM conversation_message_attachments link JOIN attachments a ON a.id=link.attachment_id
                WHERE link.message_id=m.message_id AND a.user_id=m.user_id),'[]'::jsonb) AS attachments,
              NOT EXISTS(SELECT 1 FROM conversation_messages hidden WHERE hidden.user_id=m.user_id AND hidden.session_id=m.session_id
                AND hidden.request_id=m.request_id AND (hidden.deleted_at IS NOT NULL OR hidden.retention_until<=NOW())) AS native_allowed
            FROM conversation_messages m JOIN conversation_sessions s ON s.session_id=m.session_id
            WHERE m.user_id=:user AND m.deleted_at IS NULL AND m.retention_until>NOW()
              AND s.close_reason IS DISTINCT FROM 'deleted' AND s.close_reason IS DISTINCT FROM 'cleared'
              AND (CAST(:session AS uuid) IS NULL OR m.session_id=CAST(:session AS uuid))
              AND (CAST(:exclude AS uuid) IS NULL OR m.request_id<>CAST(:exclude AS uuid))
            ORDER BY m.created_at DESC,m.sequence_no DESC LIMIT :limit
            """)
        .param("user", user)
        .param("session", session)
        .param(
            "exclude",
            excludeRequest == null ? null : StableIds.request(user, excludeRequest).toString())
        .param("limit", Math.min(1000, Math.max(1, limit)))
        .query()
        .listOfRows()
        .reversed()
        .stream()
        .map(this::decode)
        .toList();
  }

  public void rename(String user, String session, String title) {
    requireSession(user, session);
    jdbc.sql(
            "INSERT INTO chat_session_titles(user_id,session_id,title) VALUES(:user,:session,:title) ON CONFLICT(user_id,session_id) DO UPDATE SET title=EXCLUDED.title,updated_at=NOW()")
        .param("user", user)
        .param("session", session)
        .param("title", title)
        .update();
  }

  public void delete(String user, String session) {
    requireSession(user, session);
    jdbc.sql(
            "SELECT request_id FROM supervisor_runs WHERE user_id=:user AND session_id=:session FOR UPDATE")
        .param("user", user)
        .param("session", UUID.fromString(session))
        .query(String.class)
        .list();
    jdbc.sql(
            "UPDATE supervisor_runs SET cancel_requested=TRUE,status=CASE WHEN status='running' THEN 'interrupted' ELSE status END WHERE user_id=:user AND session_id=:session")
        .param("user", user)
        .param("session", UUID.fromString(session))
        .update();
    jdbc.sql(
            "UPDATE conversation_sessions SET status='closed',close_reason='deleted',closed_at=NOW() WHERE user_id=:user AND session_id=:session")
        .param("user", user)
        .param("session", UUID.fromString(session))
        .update();
    jdbc.sql(
            "UPDATE conversation_messages SET deleted_at=NOW() WHERE user_id=:user AND session_id=:session")
        .param("user", user)
        .param("session", UUID.fromString(session))
        .update();
    jdbc.sql("DELETE FROM active_trip_contexts WHERE user_id=:user AND session_id=:session")
        .param("user", user)
        .param("session", session)
        .update();
  }

  public Map<String, Object> append(
      String user, String session, String request, SessionService.Message body) {
    requireSession(user, session);
    UUID rid = StableIds.request(user, request);
    var sid = UUID.fromString(session);
    jdbc.sql(
            "SELECT session_id FROM conversation_sessions WHERE user_id=:user AND session_id=:session AND status='active' FOR UPDATE")
        .param("user", user)
        .param("session", sid)
        .query(UUID.class)
        .optional()
        .orElseThrow(() -> new ApiException(409, "SESSION_CLOSED", "会话已关闭"));
    var existing =
        jdbc.sql(
                "SELECT * FROM conversation_messages WHERE user_id=:user AND request_id=:request AND role=:role")
            .param("user", user)
            .param("request", rid)
            .param("role", body.role())
            .query()
            .listOfRows();
    boolean inserted = existing.isEmpty();
    Map<String, Object> row;
    if (!inserted) {
      row = existing.getFirst();
      if (!sid.equals(row.get("session_id")) || !body.content().equals(row.get("content")))
        throw new ApiException(409, "REQUEST_CONFLICT", "请求ID已用于其他消息");
    } else {
      long sequence =
          jdbc.sql(
                  "UPDATE conversation_sessions SET last_sequence=last_sequence+1,message_count=message_count+1,last_active_at=NOW() WHERE session_id=:id RETURNING last_sequence")
              .param("id", sid)
              .query(Long.class)
              .single();
      row =
          jdbc.sql(
                  """
                INSERT INTO conversation_messages(message_id,request_id,turn_id,session_id,user_id,sequence_no,role,content,content_type,retention_until,answer_document,presentation_document)
                VALUES(:id,:request,:turn,:session,:user,:sequence,:role,:content,:type,NOW()+INTERVAL '14 days',CAST(:answer AS jsonb),CAST(:presentation AS jsonb)) RETURNING *
                """)
              .param("id", UUID.randomUUID())
              .param("request", rid)
              .param("turn", StableIds.turn(user, rid))
              .param("session", sid)
              .param("user", user)
              .param("sequence", sequence)
              .param("role", body.role())
              .param("content", body.content())
              .param("type", body.contentType())
              .param(
                  "answer",
                  body.answerDocument() == null ? null : json.write(body.answerDocument()))
              .param(
                  "presentation",
                  body.presentationDocument() == null
                      ? null
                      : json.write(body.presentationDocument()))
              .query()
              .singleRow();
      jdbc.sql(
              "INSERT INTO memory_versions(user_id,namespace,version) VALUES(:user,'messages',1) ON CONFLICT(user_id,namespace) DO UPDATE SET version=memory_versions.version+1,updated_at=NOW()")
          .param("user", user)
          .update();
      jdbc.sql(
              "INSERT INTO user_statistics(user_id,total_messages) VALUES(:user,1) ON CONFLICT(user_id) DO UPDATE SET total_messages=user_statistics.total_messages+1,updated_at=NOW()")
          .param("user", user)
          .update();
    }
    var ids = body.attachmentIds();
    if (!ids.isEmpty()) {
      var found =
          jdbc.sql(
                  "SELECT id FROM attachments WHERE user_id=:user AND status='ready' AND id IN(:ids) AND (expires_at IS NULL OR expires_at>NOW()) FOR UPDATE")
              .param("user", user)
              .param("ids", ids)
              .query(String.class)
              .list();
      if (!new HashSet<>(found).equals(new HashSet<>(ids)))
        throw new ApiException(400, "ATTACHMENT_BINDING_FAILED", "附件不可用或不属于当前用户");
    }
    if ("user".equals(body.role())) {
      var bound =
          jdbc.sql(
                  "SELECT attachment_id FROM conversation_message_attachments WHERE message_id=:id")
              .param("id", row.get("message_id"))
              .query(String.class)
              .list();
      if (!inserted && !new HashSet<>(bound).equals(new HashSet<>(ids)))
        throw new ApiException(409, "REQUEST_CONFLICT", "重试改变了附件集合");
      for (String id : ids)
        jdbc.sql(
                "INSERT INTO conversation_message_attachments(message_id,attachment_id) VALUES(:message,:attachment) ON CONFLICT DO NOTHING")
            .param("message", row.get("message_id"))
            .param("attachment", id)
            .update();
    }
    return decode(row);
  }

  private Map<String, Object> decode(Map<String, Object> row) {
    var result = new LinkedHashMap<>(row);
    for (var entry : result.entrySet()) {
      Object v = entry.getValue();
      if (v instanceof UUID) entry.setValue(v.toString());
      if (v instanceof java.sql.Timestamp timestamp)
        entry.setValue(timestamp.toInstant().toString());
      if (Set.of("answer_document", "presentation_document").contains(entry.getKey()) && v != null)
        entry.setValue(json.read(v));
      if ("attachments".equals(entry.getKey()) && v != null) entry.setValue(json.read(v));
    }
    if (result.containsKey("role")) result.put("timestamp", result.get("created_at"));
    return result;
  }
}
