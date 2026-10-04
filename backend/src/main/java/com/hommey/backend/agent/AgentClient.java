package com.hommey.backend.agent;

import com.hommey.backend.security.TokenService;
import io.netty.channel.ChannelOption;
import java.time.Duration;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.*;
import org.springframework.http.client.reactive.ReactorClientHttpConnector;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.*;
import reactor.netty.http.client.HttpClient;

@Component
public class AgentClient {
  private static final Logger LOG = LoggerFactory.getLogger(AgentClient.class);
  private static final ParameterizedTypeReference<Map<String, Object>> OBJECT =
      new ParameterizedTypeReference<>() {};
  private final WebClient client;
  private final TokenService tokens;

  public AgentClient(WebClient.Builder builder, AgentProperties props, TokenService tokens) {
    var http =
        HttpClient.create()
            .option(ChannelOption.CONNECT_TIMEOUT_MILLIS, 5000)
            .responseTimeout(Duration.ofSeconds(150));
    this.client =
        builder
            .baseUrl(props.baseUrl())
            .clientConnector(new ReactorClientHttpConnector(http))
            .codecs(c -> c.defaultCodecs().maxInMemorySize(26 * 1024 * 1024))
            .build();
    this.tokens = tokens;
  }

  public String credential(String user, String role, String session, String request) {
    return tokens.agent(user, role, session, request);
  }

  public Flux<Map<String, Object>> stream(
      String path, String credential, String request, Object body) {
    return client
        .post()
        .uri(path)
        .headers(
            h -> {
              h.setBearerAuth(credential);
              h.set("X-Request-ID", request);
            })
        .bodyValue(body)
        .retrieve()
        .bodyToFlux(OBJECT)
        .timeout(Duration.ofSeconds(150))
        .doOnCancel(
            () ->
                json(
                        HttpMethod.POST,
                        path.replace("/chat/stream", "/orchestration/interrupt"),
                        credential,
                        Map.of(
                            "client_request_id",
                            request,
                            "session_id",
                            ((Map<?, ?>) body).get("session_id")))
                    .timeout(Duration.ofSeconds(5))
                    .subscribe(
                        v -> {},
                        e ->
                            LOG.warn(
                                "agent_cancel_failed request_id={} error_type={}",
                                request,
                                e.getClass().getSimpleName())));
  }

  public Mono<Map<String, Object>> json(
      HttpMethod method, String path, String credential, Object body) {
    var request = client.method(method).uri(path).headers(h -> h.setBearerAuth(credential));
    return (body == null ? request : request.bodyValue(body)).retrieve().bodyToMono(OBJECT);
  }

  public Mono<ResponseEntity<byte[]>> forward(
      HttpMethod method, String path, String credential, MediaType contentType, Object body) {
    var request = client.method(method).uri(path).headers(h -> h.setBearerAuth(credential));
    if (contentType != null) request.contentType(contentType);
    return (body == null ? request : request.bodyValue(body)).retrieve().toEntity(byte[].class);
  }

  public Mono<ResponseEntity<byte[]>> multipart(
      HttpMethod method,
      String path,
      String credential,
      org.springframework.util.MultiValueMap<String, org.springframework.http.HttpEntity<?>> body) {
    return client
        .method(method)
        .uri(path)
        .headers(h -> h.setBearerAuth(credential))
        .body(org.springframework.web.reactive.function.BodyInserters.fromMultipartData(body))
        .retrieve()
        .toEntity(byte[].class);
  }
}
