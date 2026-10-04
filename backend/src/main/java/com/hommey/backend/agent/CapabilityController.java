package com.hommey.backend.agent;

import com.hommey.backend.security.AccessPolicy;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.*;
import org.springframework.http.client.MultipartBodyBuilder;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartHttpServletRequest;
import reactor.core.publisher.Mono;

@RestController
public class CapabilityController {
  private final AccessPolicy access;
  private final AgentClient agent;

  public CapabilityController(AccessPolicy access, AgentClient agent) {
    this.access = access;
    this.agent = agent;
  }

  @RequestMapping(
      value = {
        "/api/{user}/attachments",
        "/api/{user}/attachments/{attachment}",
        "/api/{user}/attachments/{attachment}/content",
        "/api/{user}/places/suggest",
        "/api/{user}/places/map",
        "/api/{user}/asr/transcribe"
      },
      method = {RequestMethod.GET, RequestMethod.POST, RequestMethod.DELETE})
  public Mono<ResponseEntity<byte[]>> user(
      @PathVariable String user, @AuthenticationPrincipal Jwt jwt, HttpServletRequest request)
      throws Exception {
    var account = access.requireUser(jwt, user);
    return forward(
        request,
        agent.credential(
            user, account.role(), null, String.valueOf(request.getAttribute("requestId"))));
  }

  @RequestMapping(
      value = {
        "/api/intents",
        "/api/admin/skills",
        "/api/admin/skills/{skill}",
        "/api/knowledge/documents",
        "/api/knowledge/documents/{*document}",
        "/api/knowledge/refresh",
        "/api/knowledge/refresh/status"
      },
      method = {RequestMethod.GET, RequestMethod.POST})
  public Mono<ResponseEntity<byte[]>> catalog(
      @AuthenticationPrincipal Jwt jwt, HttpServletRequest request) throws Exception {
    var account = access.requireUser(jwt, jwt.getSubject());
    String path = request.getRequestURI();
    if (path.startsWith("/api/admin/")
        || path.startsWith("/api/knowledge/refresh")
        || (path.startsWith("/api/knowledge/") && !"GET".equals(request.getMethod())))
      access.requireAdmin(jwt);
    return forward(
        request,
        agent.credential(
            jwt.getSubject(),
            account.role(),
            null,
            String.valueOf(request.getAttribute("requestId"))));
  }

  private Mono<ResponseEntity<byte[]>> forward(HttpServletRequest request, String token)
      throws Exception {
    String path =
        "/internal/capabilities"
            + request.getRequestURI()
            + (request.getQueryString() == null ? "" : "?" + request.getQueryString());
    HttpMethod method = HttpMethod.valueOf(request.getMethod());
    if (request instanceof MultipartHttpServletRequest multipart) {
      var builder = new MultipartBodyBuilder();
      multipart
          .getMultiFileMap()
          .forEach((name, files) -> files.forEach(file -> builder.part(name, file.getResource())));
      multipart
          .getParameterMap()
          .forEach(
              (name, values) -> {
                for (String value : values) builder.part(name, value);
              });
      return agent.multipart(method, path, token, builder.build());
    }
    byte[] body = request.getInputStream().readNBytes(26 * 1024 * 1024 + 1);
    if (body.length > 26 * 1024 * 1024)
      throw new com.hommey.backend.common.ApiException(413, "PAYLOAD_TOO_LARGE", "请求内容过大");
    return agent.forward(
        method,
        path,
        token,
        request.getContentType() == null
            ? null
            : MediaType.parseMediaType(request.getContentType()),
        body.length == 0 ? null : body);
  }
}
