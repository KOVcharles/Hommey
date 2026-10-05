package com.hommey.backend.agent;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;

import com.hommey.backend.security.TokenService;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpMethod;
import org.springframework.http.client.MultipartBodyBuilder;
import org.springframework.web.reactive.function.client.WebClient;

class AgentClientTest {
  @Test
  void forwardsAlreadyEncodedChineseAndReservedCharactersOnce() throws Exception {
    checkForward(false);
  }

  @Test
  void multipartForwardPreservesEncodedQueryAndBasePath() throws Exception {
    checkForward(true);
  }

  private void checkForward(boolean multipart) throws Exception {
    var received = new AtomicReference<java.net.URI>();
    var authorization = new AtomicReference<String>();
    var body = new AtomicReference<String>();
    var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
    server.createContext(
        "/",
        exchange -> {
          received.set(exchange.getRequestURI());
          authorization.set(exchange.getRequestHeaders().getFirst("Authorization"));
          body.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
          exchange.getResponseHeaders().set("Content-Type", "application/json");
          byte[] response = "{\"items\":[]}".getBytes(StandardCharsets.UTF_8);
          exchange.sendResponseHeaders(200, response.length);
          try (var out = exchange.getResponseBody()) {
            out.write(response);
          }
        });
    server.start();
    try {
      var client =
          new AgentClient(
              WebClient.builder(),
              new AgentProperties("http://127.0.0.1:" + server.getAddress().getPort() + "/gateway"),
              mock(TokenService.class));
      String query =
          "city="
              + URLEncoder.encode("上海", StandardCharsets.UTF_8)
              + "&keyword="
              + URLEncoder.encode("上海大学 & 科技+园 100% #1", StandardCharsets.UTF_8);
      String path = "/internal/capabilities/api/1/places/suggest?" + query;
      if (multipart) {
        var parts = new MultipartBodyBuilder();
        parts.part("note", "地点材料");
        assertThat(
                client
                    .multipart(HttpMethod.POST, path, "test-token", parts.build())
                    .block(Duration.ofSeconds(5))
                    .getStatusCode()
                    .value())
            .isEqualTo(200);
        assertThat(body.get()).contains("地点材料");
      } else {
        assertThat(
                client
                    .forward(HttpMethod.GET, path, "test-token", null, null)
                    .block(Duration.ofSeconds(5))
                    .getStatusCode()
                    .value())
            .isEqualTo(200);
      }
      assertThat(received.get().getRawPath())
          .isEqualTo("/gateway/internal/capabilities/api/1/places/suggest");
      assertThat(received.get().getRawQuery()).isEqualTo(query);
      assertThat(authorization.get()).isEqualTo("Bearer test-token");
    } finally {
      server.stop(0);
    }
  }
}
