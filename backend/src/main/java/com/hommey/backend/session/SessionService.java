package com.hommey.backend.session;

import jakarta.validation.constraints.*;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class SessionService {
  public record Message(
      @NotBlank @Pattern(regexp = "user|assistant") String role,
      @NotNull @Size(max = 100000) String content,
      @NotBlank @Size(max = 32) String contentType,
      @NotNull @Size(max = 20) List<String> attachmentIds,
      Map<String, Object> answerDocument,
      Map<String, Object> presentationDocument) {}

  private final SessionRepository repository;

  public SessionService(SessionRepository repository) {
    this.repository = repository;
  }

  public void require(String user, String session) {
    repository.requireSession(user, session);
  }

  public Object list(String user) {
    return Map.of("sessions", repository.sessions(user));
  }

  @Transactional
  public Object create(String user) {
    String id = repository.create(user);
    return Map.of("session_id", id, "messages", List.of(), "title", "新会话");
  }

  public Object get(String user, String session) {
    repository.requireSession(user, session);
    var rows = repository.messages(user, session, 1000, null);
    for (int index = 0; index < rows.size(); index++) {
      var row = rows.get(index);
      if ("assistant".equals(row.get("role"))
          && row.get("presentation_document") instanceof Map<?, ?> document
          && Set.of("trip_intake", "information_request").contains(document.get("type"))) {
        var presentation = new LinkedHashMap<String, Object>();
        document.forEach((key, value) -> presentation.put(key.toString(), value));
        var interaction = presentation.getOrDefault("interaction_id", row.get("request_id"));
        if (interaction == null) interaction = row.get("request_id");
        presentation.put("interaction_id", interaction);
        presentation.put("archived", index != rows.size() - 1 || interaction == null);
        if ("information_request".equals(document.get("type"))
            && index + 1 < rows.size()
            && "form_submission".equals(rows.get(index + 1).get("content_type")))
          presentation.put("submitted_text", rows.get(index + 1).get("content"));
        row.put("presentation_document", presentation);
      }
    }
    return Map.of(
        "session_id", session, "title", repository.title(user, session), "messages", rows);
  }

  public List<Map<String, Object>> messages(
      String user, String session, int limit, String exclude) {
    return repository.messages(user, session, limit, exclude);
  }

  @Transactional
  public void rename(String user, String session, String title) {
    repository.rename(user, session, title);
  }

  @Transactional
  public void delete(String user, String session) {
    repository.delete(user, session);
  }

  @Transactional
  public void clear(String user) {
    for (var id : repository.allSessionIds(user)) repository.delete(user, id);
  }

  @Transactional
  public Object append(String user, String session, String request, Message message) {
    return repository.append(user, session, request, message);
  }
}
