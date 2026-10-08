package com.hommey.backend.agent;

import com.hommey.backend.common.ApiException;
import com.hommey.backend.security.AccessPolicy;
import com.hommey.backend.session.SessionService;
import jakarta.servlet.http.HttpServletRequest;
import java.util.*;
import org.springframework.http.*;
import org.springframework.web.bind.annotation.*;
import reactor.core.publisher.*;

@RestController
@RequestMapping("/api/{user}")
public class ChatController {
  private final AccessPolicy access;
  private final SessionService sessions;
  private final AgentClient agent;

  public ChatController(AccessPolicy access, SessionService sessions, AgentClient agent) {
    this.access = access;
    this.sessions = sessions;
    this.agent = agent;
  }

  private String session(Map<String, Object> body) {
    Object value = body.get("session_id");
    if (!(value instanceof String text)) throw new ApiException(400, "SESSION_REQUIRED", "请先选择会话");
    UUID.fromString(text);
    return text;
  }

  private String request(Map<String, Object> body, HttpServletRequest request) {
    String id = String.valueOf(request.getAttribute("requestId"));
    if (body.get("client_request_id") != null && !id.equals(body.get("client_request_id")))
      throw new ApiException(400, "REQUEST_CONFLICT", "请求ID不一致");
    return id;
  }

  @PostMapping(value = "/chat/stream", produces = "application/x-ndjson")
  public Flux<Map<String, Object>> stream(
      @PathVariable String user, @RequestBody Map<String, Object> body, HttpServletRequest req) {
    var account = access.requireUser(user);
    String session = session(body), id = request(body, req);
    sessions.require(user, session);
    return agent.stream(
            "/internal/capabilities/api/" + user + "/chat/stream",
            agent.credential(user, account.role(), session, id),
            id,
            body)
        .onErrorResume(
            ex ->
                Flux.just(
                    Map.of(
                        "type",
                        "error",
                        "code",
                        "AGENT_UNAVAILABLE",
                        "message",
                        "AI服务暂不可用，请稍后重试",
                        "request_id",
                        id,
                        "retryable",
                        true)));
  }

  @PostMapping("/chat")
  public Mono<?> chat(
      @PathVariable String user, @RequestBody Map<String, Object> body, HttpServletRequest req) {
    var account = access.requireUser(user);
    String session = session(body), id = request(body, req);
    sessions.require(user, session);
    return agent.json(
        HttpMethod.POST,
        "/internal/capabilities/api/" + user + "/chat",
        agent.credential(user, account.role(), session, id),
        body);
  }

  @PostMapping("/orchestration/interrupt")
  public Mono<?> interrupt(@PathVariable String user, @RequestBody Map<String, Object> body) {
    var account = access.requireUser(user);
    String session = session(body);
    sessions.require(user, session);
    Object request = body.get("client_request_id");
    if (!(request instanceof String id) || !id.matches("[A-Za-z0-9_-]{1,128}"))
      throw new ApiException(400, "BAD_REQUEST", "请求ID无效");
    return agent.json(
        HttpMethod.POST,
        "/internal/capabilities/api/" + user + "/orchestration/interrupt",
        agent.credential(user, account.role(), session, id),
        body);
  }

  @GetMapping("/sessions/{session}/execution-plans")
  public Mono<?> plans(@PathVariable String user, @PathVariable String session) {
    var account = access.requireUser(user);
    sessions.require(user, session);
    return agent.json(
        HttpMethod.GET,
        "/internal/capabilities/api/" + user + "/sessions/" + session + "/execution-plans",
        agent.credential(user, account.role(), session, null),
        null);
  }
}
