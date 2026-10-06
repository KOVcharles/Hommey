package com.hommey.backend.common;

import cn.dev33.satoken.exception.NotLoginException;
import cn.dev33.satoken.exception.NotPermissionException;
import cn.dev33.satoken.exception.NotRoleException;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class ApiErrors {
  private static final Logger LOG = LoggerFactory.getLogger(ApiErrors.class);

  public static Map<String, Object> body(String code, String message, HttpServletRequest request) {
    return Map.of(
        "success",
        false,
        "error",
        Map.of(
            "code",
            code,
            "message",
            message,
            "details",
            Map.of(),
            "request_id",
            String.valueOf(request.getAttribute("requestId"))));
  }

  public static void write(
      ObjectMapper mapper,
      HttpServletRequest req,
      HttpServletResponse res,
      int status,
      String code,
      String message)
      throws IOException {
    res.setStatus(status);
    res.setContentType("application/json;charset=UTF-8");
    mapper.writeValue(res.getOutputStream(), body(code, message, req));
  }

  @ExceptionHandler(ApiException.class)
  ResponseEntity<?> business(ApiException ex, HttpServletRequest req) {
    return ResponseEntity.status(ex.status()).body(body(ex.code(), ex.getMessage(), req));
  }

  @ExceptionHandler(NotLoginException.class)
  ResponseEntity<?> notLoggedIn(NotLoginException ex, HttpServletRequest req) {
    return ResponseEntity.status(401).body(body("UNAUTHORIZED", "登录已失效，请重新登录", req));
  }

  @ExceptionHandler({NotRoleException.class, NotPermissionException.class})
  ResponseEntity<?> forbidden(Exception ex, HttpServletRequest req) {
    return ResponseEntity.status(403).body(body("FORBIDDEN", "无权访问此资源", req));
  }

  @ExceptionHandler(org.springframework.data.redis.RedisConnectionFailureException.class)
  ResponseEntity<?> authUnavailable(Exception ex, HttpServletRequest req) {
    return ResponseEntity.status(503).body(body("AUTH_UNAVAILABLE", "认证服务暂不可用，请稍后重试", req));
  }

  @ExceptionHandler({
    MethodArgumentNotValidException.class,
    HttpMessageNotReadableException.class,
    IllegalArgumentException.class,
    org.springframework.web.bind.MissingServletRequestParameterException.class,
    org.springframework.web.method.annotation.MethodArgumentTypeMismatchException.class,
    org.springframework.web.method.annotation.HandlerMethodValidationException.class
  })
  ResponseEntity<?> invalid(Exception ex, HttpServletRequest req) {
    return ResponseEntity.badRequest().body(body("BAD_REQUEST", "请求参数无效", req));
  }

  @ExceptionHandler(
      org.springframework.web.reactive.function.client.WebClientResponseException.class)
  ResponseEntity<?> upstream(
      org.springframework.web.reactive.function.client.WebClientResponseException ex,
      HttpServletRequest req) {
    var status = ex.getStatusCode();
    if (status.is4xxClientError()) {
      try {
        var error = ex.getResponseBodyAs(Map.class);
        if (error != null
            && error.get("error") instanceof Map<?, ?> detail
            && detail.get("code") instanceof String code
            && detail.get("message") instanceof String message)
          return ResponseEntity.status(status).body(body(code, message, req));
      } catch (Exception ignored) {
        /* Untrusted upstream error content is never returned verbatim. */
      }
      return ResponseEntity.status(status).body(body("CAPABILITY_REJECTED", "请求未被 AI 服务接受", req));
    }
    return ResponseEntity.status(502).body(body("AGENT_UNAVAILABLE", "AI 服务暂时不可用，请稍后重试", req));
  }

  @ExceptionHandler(
      org.springframework.web.reactive.function.client.WebClientRequestException.class)
  ResponseEntity<?> unavailable(Exception ex, HttpServletRequest req) {
    return ResponseEntity.status(502).body(body("AGENT_UNAVAILABLE", "AI 服务暂时不可用，请稍后重试", req));
  }

  @ExceptionHandler(Exception.class)
  ResponseEntity<?> unexpected(Exception ex, HttpServletRequest req) {
    LOG.error(
        "request_failed request_id={} error_type={}",
        req.getAttribute("requestId"),
        ex.getClass().getSimpleName());
    return ResponseEntity.internalServerError().body(body("INTERNAL_ERROR", "服务暂时不可用，请稍后重试", req));
  }
}
