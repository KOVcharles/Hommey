package com.hommey.backend;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.hommey.backend.agent.TripVersion;
import com.hommey.backend.auth.RegistrationService;
import com.hommey.backend.common.StableIds;
import com.hommey.backend.security.TokenService;
import com.sun.net.httpserver.HttpServer;
import java.io.*;
import java.net.*;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.*;
import java.util.*;
import org.altcha.altcha.v2.Altcha;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.*;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.*;
import org.testcontainers.utility.DockerImageName;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@Testcontainers
class BackendIntegrationTest {
  @Container
  static final GenericContainer<?> REDIS =
      new GenericContainer<>(DockerImageName.parse("redis:7-alpine")).withExposedPorts(6379);

  private static final java.util.concurrent.atomic.AtomicReference<String> MAIL_CODE =
      new java.util.concurrent.atomic.AtomicReference<>();

  @Container
  static final PostgreSQLContainer<?> PG =
      new PostgreSQLContainer<>(
              DockerImageName.parse("pgvector/pgvector:0.8.6-pg16-bookworm")
                  .asCompatibleSubstituteFor("postgres"))
          .withUsername("hommey")
          .withPassword("test-business-password")
          .withInitScript("init-ai-role.sql");

  private static final Path PRIVATE, PUBLIC;
  private static final HttpServer AI;

  static {
    try {
      var generator = KeyPairGenerator.getInstance("RSA");
      generator.initialize(2048);
      var pair = generator.generateKeyPair();
      PRIVATE = Files.createTempFile("hommey-test-private", ".pem");
      PUBLIC = Files.createTempFile("hommey-test-public", ".pem");
      PRIVATE.toFile().deleteOnExit();
      PUBLIC.toFile().deleteOnExit();
      Files.writeString(PRIVATE, pem("PRIVATE KEY", pair.getPrivate().getEncoded()));
      Files.writeString(PUBLIC, pem("PUBLIC KEY", pair.getPublic().getEncoded()));
      AI = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
      AI.createContext(
          "/internal/capabilities/api/1/chat/stream",
          exchange -> {
            exchange.getRequestBody().readAllBytes();
            exchange.getResponseHeaders().set("Content-Type", "application/x-ndjson");
            exchange.sendResponseHeaders(200, 0);
            try (var out = exchange.getResponseBody()) {
              out.write(
                  "{\"type\":\"chunk\",\"text\":\"first\"}\n".getBytes(StandardCharsets.UTF_8));
              out.flush();
              try {
                Thread.sleep(800);
              } catch (InterruptedException ex) {
                Thread.currentThread().interrupt();
              }
              out.write("{\"type\":\"done\"}\n".getBytes(StandardCharsets.UTF_8));
              out.flush();
            }
          });
      AI.start();
      AI.createContext(
          "/test-mail",
          exchange -> {
            var mail = new ObjectMapper().readValue(exchange.getRequestBody(), Map.class);
            var code =
                java.util.regex.Pattern.compile("[0-9]{6}").matcher(mail.get("text").toString());
            if (code.find()) MAIL_CODE.set(code.group());
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            byte[] response = "{\"id\":\"test-mail\"}".getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, response.length);
            try (var out = exchange.getResponseBody()) {
              out.write(response);
            }
          });
    } catch (Exception ex) {
      throw new ExceptionInInitializerError(ex);
    }
  }

  static String pem(String kind, byte[] bytes) {
    return "-----BEGIN "
        + kind
        + "-----\n"
        + Base64.getMimeEncoder(64, new byte[] {'\n'}).encodeToString(bytes)
        + "\n-----END "
        + kind
        + "-----\n";
  }

  @DynamicPropertySource
  static void properties(DynamicPropertyRegistry properties) {
    properties.add("spring.datasource.url", PG::getJdbcUrl);
    properties.add("spring.datasource.username", PG::getUsername);
    properties.add("spring.datasource.password", PG::getPassword);
    properties.add("hommey.security.private-key", () -> "file:" + PRIVATE);
    properties.add("hommey.security.public-key", () -> "file:" + PUBLIC);
    properties.add("hommey.agent.base-url", () -> "http://127.0.0.1:" + AI.getAddress().getPort());
    properties.add("hommey.registration.altcha-secret", () -> "test-only-captcha-secret");
    properties.add("management.health.redis.enabled", () -> false);
    properties.add("spring.data.redis.host", REDIS::getHost);
    properties.add("spring.data.redis.port", () -> REDIS.getMappedPort(6379));
    properties.add(
        "hommey.registration.resend-url",
        () -> "http://127.0.0.1:" + AI.getAddress().getPort() + "/test-mail");
    properties.add("hommey.registration.resend-api-key", () -> "test-only-key");
  }

  @Autowired TestRestTemplate http;
  @Autowired JdbcClient jdbc;
  @Autowired TokenService tokens;
  @Autowired PasswordEncoder passwords;
  @Autowired TripVersion versions;
  @Autowired ObjectMapper mapper;
  @Autowired StringRedisTemplate redis;
  @LocalServerPort int port;

  @BeforeEach
  void reset() {
    try (var connection = redis.getConnectionFactory().getConnection()) {
      connection.serverCommands().flushDb();
    }
    MAIL_CODE.set(null);
    jdbc.sql("TRUNCATE invite_codes").update();
    jdbc.sql("TRUNCATE users RESTART IDENTITY CASCADE").update();
    jdbc.sql(
            "TRUNCATE conversation_sessions,business_operation_receipts,user_personal_profiles,user_travel_preferences,user_preferences,active_trip_contexts,chat_session_titles,memory_versions,user_statistics CASCADE")
        .update();
    for (String email : List.of("one@example.com", "two@example.com"))
      jdbc.sql("INSERT INTO users(email,password_hash) VALUES(:email,:password)")
          .param("email", email)
          .param("password", passwords.encode("password123"))
          .update();
  }

  ResponseEntity<Map> call(HttpMethod method, String path, String token, Object body) {
    var headers = new HttpHeaders();
    if (token != null) headers.setBearerAuth(token);
    return http.exchange(path, method, new HttpEntity<>(body, headers), Map.class);
  }

  String session() {
    return (String)
        call(HttpMethod.POST, "/api/1/sessions", tokens.issue("1", "access"), null)
            .getBody()
            .get("session_id");
  }

  @Test
  void loginAndAuthorizationUseRealSignedTokens() {
    var result =
        call(
            HttpMethod.POST,
            "/auth/login",
            null,
            Map.of("email", "one@example.com", "password", "password123"));
    assertThat(result.getStatusCode().value()).isEqualTo(200);
    String access = (String) result.getBody().get("access_token");
    assertThat(call(HttpMethod.GET, "/api/1/profile", access, null).getStatusCode().value())
        .isEqualTo(200);
    assertThat(call(HttpMethod.GET, "/api/2/profile", access, null).getStatusCode().value())
        .isEqualTo(403);
    assertThat(
            call(HttpMethod.GET, "/api/1/profile", tokens.issue("1", "refresh"), null)
                .getStatusCode()
                .value())
        .isEqualTo(403);
    assertThat(
            call(HttpMethod.GET, "/internal/business/users/1/preferences", access, null)
                .getStatusCode()
                .value())
        .isEqualTo(403);
    assertThat(
            call(HttpMethod.GET, "/api/1/profile", tokens.agent("1", "user", null, null), null)
                .getStatusCode()
                .value())
        .isEqualTo(403);
    assertThat(call(HttpMethod.GET, "/api/1/profile", null, null).getStatusCode().value())
        .isEqualTo(401);
  }

  @Test
  void profileValidationAndOptimisticConcurrency() {
    var valid =
        Map.of(
            "profile",
            Map.of("basic_info", Map.of("personnel_category", "student")),
            "revision",
            0);
    var token = tokens.issue("1", "access");
    assertThat(call(HttpMethod.PUT, "/api/1/profile", token, valid).getStatusCode().value())
        .isEqualTo(200);
    assertThat(call(HttpMethod.PUT, "/api/1/profile", token, valid).getStatusCode().value())
        .isEqualTo(409);
    var invalid =
        Map.of(
            "profile",
            Map.of(
                "basic_info",
                Map.of("personnel_category", "student"),
                "policy_identity",
                Map.of("staff_grade", 5)),
            "revision",
            1);
    assertThat(call(HttpMethod.PUT, "/api/1/profile", token, invalid).getStatusCode().value())
        .isEqualTo(400);
    var unknown = Map.of("profile", Map.of("unexpected", true), "revision", 1);
    assertThat(call(HttpMethod.PUT, "/api/1/profile", token, unknown).getStatusCode().value())
        .isEqualTo(400);
  }

  @Test
  void sessionOwnershipAndPageCompatibility() {
    String session = session();
    assertThat(
            call(HttpMethod.GET, "/api/1/sessions/" + session, tokens.issue("1", "access"), null)
                .getStatusCode()
                .value())
        .isEqualTo(200);
    assertThat(
            call(HttpMethod.GET, "/api/2/sessions/" + session, tokens.issue("2", "access"), null)
                .getStatusCode()
                .value())
        .isEqualTo(404);
    String page = http.getForObject("/chat/1", String.class);
    assertThat(page)
        .contains(
            "data-user-id=\"1\"",
            "class=\"settings-sidebar\"",
            "id=\"settingsBasicPanel\"",
            "id=\"settingsFundingTab\"")
        .doesNotContain("{{ user_id", "id=\"panelLevel\"");
    String landing = http.getForObject("/", String.class);
    assertThat(landing)
        .contains("id=\"authScenery\"", "/static/auth-background.js", "href=\"/login\"")
        .doesNotContain("id=\"loginForm\"");
    String signIn = http.getForObject("/login", String.class);
    assertThat(signIn)
        .contains("id=\"loginForm\"", "href=\"/\"")
        .doesNotContain("id=\"authScenery\"");
    for (String asset :
        List.of(
            "/static/auth-background.js",
            "/static/auth-transition.js",
            "/static/auth.css",
            "/static/settings.css",
            "/static/fonts/inter-latin.woff2",
            "/static/fonts/settings-sans.woff2",
            "/static/travel/airport.webp",
            "/static/travel/train.webp",
            "/static/travel/arrival.webp",
            "/static/travel/hotel.webp"))
      assertThat(http.getForEntity(asset, byte[].class).getStatusCode().value()).isEqualTo(200);
    assertThat(http.getForEntity("/static/app.js", String.class).getStatusCode().value())
        .isEqualTo(200);
  }

  @Test
  void messageRetriesAreIdempotentAndScoped() {
    String session = session(), credential = tokens.agent("1", "user", session, "req-1");
    var message =
        Map.of(
            "role", "user", "content", "去重庆", "content_type", "text", "attachment_ids", List.of());
    var first = call(HttpMethod.POST, "/internal/business/users/1/messages", credential, message);
    var second = call(HttpMethod.POST, "/internal/business/users/1/messages", credential, message);
    assertThat(first.getStatusCode().value()).isEqualTo(200);
    assertThat(second.getBody().get("message_id")).isEqualTo(first.getBody().get("message_id"));
    assertThat(jdbc.sql("SELECT COUNT(*) FROM conversation_messages").query(Integer.class).single())
        .isEqualTo(1);
    var changed = new HashMap<String, Object>(message);
    changed.put("content", "不同内容");
    assertThat(
            call(HttpMethod.POST, "/internal/business/users/1/messages", credential, changed)
                .getStatusCode()
                .value())
        .isEqualTo(409);
    assertThat(
            call(
                    HttpMethod.GET,
                    "/internal/business/users/1/messages?session_id=" + UUID.randomUUID(),
                    credential,
                    null)
                .getStatusCode()
                .value())
        .isEqualTo(403);
  }

  @Test
  void businessTransactionReceiptsProtectRetriesVersionsAndCancellation() {
    String session = session();
    var sid = UUID.fromString(session);
    String request = "mutation-1", owner = "owner-1";
    jdbc.sql(
            "INSERT INTO supervisor_runs(user_id,session_id,request_id,input_hash,owner) VALUES('1',:session,:request,'hash',:owner)")
        .param("session", sid)
        .param("request", request)
        .param("owner", owner)
        .update();
    var body = new LinkedHashMap<String, Object>();
    body.put("operation_id", "a".repeat(64));
    body.put("owner", owner);
    body.put("expected_version", versions.version(Map.of()));
    body.put("trip", Map.of("destination", "重庆"));
    body.put("preferences", Map.of("home_location", "上海"));
    body.put("action", "update");
    String credential = tokens.agent("1", "user", session, request);
    var first = call(HttpMethod.POST, "/internal/business/users/1/mutations", credential, body);
    assertThat(first.getStatusCode().value()).isEqualTo(200);
    assertThat(
            call(HttpMethod.POST, "/internal/business/users/1/mutations", credential, body)
                .getBody())
        .isEqualTo(first.getBody());
    assertThat(
            jdbc.sql("SELECT COUNT(*) FROM business_operation_receipts")
                .query(Integer.class)
                .single())
        .isEqualTo(1);
    body.put("operation_id", "b".repeat(64));
    assertThat(
            call(HttpMethod.POST, "/internal/business/users/1/mutations", credential, body)
                .getStatusCode()
                .value())
        .isEqualTo(409);
    jdbc.sql("UPDATE supervisor_runs SET cancel_requested=TRUE WHERE request_id=:request")
        .param("request", request)
        .update();
    body.put("expected_version", first.getBody().get("version"));
    assertThat(
            call(HttpMethod.POST, "/internal/business/users/1/mutations", credential, body)
                .getStatusCode()
                .value())
        .isEqualTo(409);
    assertThat(
            jdbc.sql("SELECT COUNT(*) FROM business_operation_receipts")
                .query(Integer.class)
                .single())
        .isEqualTo(1);
  }

  @Test
  void javaAndPythonIdentifiersAndVersionsMatch() {
    assertThat(StableIds.request("1", "req-1").toString())
        .isEqualTo("daf62b7a-9a7b-5a06-b098-14e20c7a4f8c");
    assertThat(versions.version(Map.of())).isEqualTo(74850268042675L);
    assertThat(
            versions.version(Map.of("destination", "重庆", "status", "active", "duration_days", 3)))
        .isEqualTo(118325858044730L);
  }

  @Test
  void registrationUsesRealCaptchaRedisAndSingleUseInvite() throws Exception {
    String invite = "HMY-TEST-ONLY-INVITE";
    jdbc.sql("INSERT INTO invite_codes(code_hash) VALUES(:hash)")
        .param("hash", RegistrationService.inviteHash(invite))
        .update();
    var response = call(HttpMethod.GET, "/auth/altcha-challenge", null, null);
    var protocol = new ObjectMapper();
    var challenge = protocol.convertValue(response.getBody(), Altcha.Challenge.class);
    var solution = Altcha.solveChallenge(challenge, Altcha.kdf("SHA-256"));
    assertThat(solution).isNotNull();
    var payload = Map.of("challenge", response.getBody(), "solution", solution);
    String compact = Base64.getEncoder().encodeToString(protocol.writeValueAsBytes(payload));
    var send = Map.of("email", "new@example.com", "invite_code", invite, "altcha", compact);
    assertThat(
            call(HttpMethod.POST, "/auth/send-verification-code", null, send)
                .getStatusCode()
                .value())
        .isEqualTo(200);
    assertThat(MAIL_CODE.get()).matches("[0-9]{6}");
    var replay = new HashMap<String, Object>(send);
    replay.put(
        "altcha",
        Base64.getEncoder()
            .encodeToString(protocol.writerWithDefaultPrettyPrinter().writeValueAsBytes(payload)));
    assertThat(
            call(HttpMethod.POST, "/auth/send-verification-code", null, replay)
                .getStatusCode()
                .value())
        .isEqualTo(400);
    var body =
        new HashMap<String, Object>(
            Map.of(
                "email",
                "new@example.com",
                "password",
                "password123",
                "invite_code",
                invite,
                "code",
                MAIL_CODE.get().equals("000000") ? "111111" : "000000"));
    assertThat(call(HttpMethod.POST, "/auth/register", null, body).getStatusCode().value())
        .isEqualTo(400);
    body.put("code", MAIL_CODE.get());
    assertThat(call(HttpMethod.POST, "/auth/register", null, body).getStatusCode().value())
        .isEqualTo(201);
    assertThat(
            jdbc.sql("SELECT COUNT(*) FROM invite_codes WHERE used_at IS NOT NULL")
                .query(Integer.class)
                .single())
        .isEqualTo(1);
    body.put("email", "different@example.com");
    assertThat(call(HttpMethod.POST, "/auth/register", null, body).getStatusCode().value())
        .isEqualTo(400);
  }

  @Test
  void onboardingKeepsExistingSensitiveValueRestrictions() {
    String token = tokens.issue("1", "access");
    for (String value : List.of("13800138000", "api_key=sk-test-secret-value"))
      assertThat(
              call(
                      HttpMethod.POST,
                      "/api/1/onboarding/preference",
                      token,
                      Map.of("key", "home_location", "value", value))
                  .getStatusCode()
                  .value())
          .isEqualTo(400);
    assertThat(jdbc.sql("SELECT COUNT(*) FROM user_preferences").query(Integer.class).single())
        .isZero();
  }

  @Test
  void profileRejectsMissingRevisionsAndScalarCoercion() {
    String token = tokens.issue("1", "access");
    for (var body :
        List.of(
            Map.of("profile", Map.of()),
            Map.of("profile", Map.of(), "revision", "0"),
            Map.of("profile", Map.of("settlement", Map.of("has_official_card", 1)), "revision", 0),
            Map.of("profile", Map.of("schema_version", 0), "revision", 0),
            Map.of(
                "profile",
                Map.of("settlement", Map.of("has_official_card", "true")),
                "revision",
                0)))
      assertThat(call(HttpMethod.PUT, "/api/1/profile", token, body).getStatusCode().value())
          .isEqualTo(400);
  }

  @Test
  void altchaChallengeKeepsTheOfficialProtocolNames() {
    var response = call(HttpMethod.GET, "/auth/altcha-challenge", null, null);
    assertThat(response.getStatusCode().value()).isEqualTo(200);
    var parameters = (Map<?, ?>) response.getBody().get("parameters");
    assertThat(parameters.containsKey("keyLength")).isTrue();
    assertThat(parameters.containsKey("key_length")).isFalse();
    assertThat(parameters.containsKey("expiresAt")).isTrue();
  }

  @Test
  void historyPreservesDocumentsTimestampsAndClearHasNoPageLimit() {
    String sid = session(), credential = tokens.agent("1", "user", sid, "card-1");
    call(
        HttpMethod.POST,
        "/internal/business/users/1/messages",
        credential,
        Map.of(
            "role",
            "assistant",
            "content",
            "补充信息",
            "content_type",
            "text",
            "attachment_ids",
            List.of(),
            "presentation_document",
            Map.of("type", "information_request")));
    call(
        HttpMethod.POST,
        "/internal/business/users/1/messages",
        tokens.agent("1", "user", sid, "form-1"),
        Map.of(
            "role",
            "user",
            "content",
            "上海",
            "content_type",
            "form_submission",
            "attachment_ids",
            List.of()));
    var response =
        call(HttpMethod.GET, "/api/1/sessions/" + sid, tokens.issue("1", "access"), null);
    var rows = (List<Map<String, Object>>) response.getBody().get("messages");
    assertThat(rows).hasSize(2);
    assertThat(rows.getFirst().get("timestamp").toString()).endsWith("Z");
    assertThat((Map<String, Object>) rows.getFirst().get("presentation_document"))
        .containsEntry("archived", true)
        .containsEntry("submitted_text", "上海");
    for (int n = 0; n < 101; n++)
      jdbc.sql(
              "INSERT INTO conversation_sessions(session_id,user_id,status) VALUES(:id,'1','active')")
          .param("id", UUID.randomUUID())
          .update();
    assertThat(
            call(HttpMethod.DELETE, "/api/1/history", tokens.issue("1", "access"), null)
                .getStatusCode()
                .value())
        .isEqualTo(200);
    assertThat(
            jdbc.sql(
                    "SELECT COUNT(*) FROM conversation_sessions WHERE close_reason IS DISTINCT FROM 'deleted'")
                .query(Integer.class)
                .single())
        .isZero();
  }

  @Test
  void aiDatabaseAccountCannotWriteBusinessTables() throws Exception {
    try (var connection =
            java.sql.DriverManager.getConnection(PG.getJdbcUrl(), "hommey_ai", "test-ai-password");
        var statement = connection.createStatement()) {
      assertThat(
              statement
                  .executeQuery(
                      "SELECT has_table_privilege(current_user,'conversation_messages','INSERT')")
                  .next())
          .isTrue();
      var permissions =
          statement.executeQuery(
              "SELECT has_table_privilege(current_user,'conversation_messages','INSERT'),has_table_privilege(current_user,'user_personal_profiles','UPDATE'),has_table_privilege(current_user,'supervisor_runs','UPDATE')");
      permissions.next();
      assertThat(permissions.getBoolean(1)).isFalse();
      assertThat(permissions.getBoolean(2)).isFalse();
      assertThat(permissions.getBoolean(3)).isTrue();
    }
  }

  @Test
  void ndjsonIsForwardedBeforeTheUpstreamCompletes() throws Exception {
    String session = session();
    var request =
        HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/api/1/chat/stream"))
            .header("Authorization", "Bearer " + tokens.issue("1", "access"))
            .header("Content-Type", "application/json")
            .header("X-Request-ID", "stream-1")
            .POST(
                HttpRequest.BodyPublishers.ofString(
                    mapper.writeValueAsString(
                        Map.of(
                            "session_id",
                            session,
                            "client_request_id",
                            "stream-1",
                            "message",
                            "你好"))))
            .build();
    try (var client = HttpClient.newHttpClient()) {
      var response = client.send(request, HttpResponse.BodyHandlers.ofInputStream());
      assertThat(response.statusCode()).isEqualTo(200);
      try (var reader =
          new BufferedReader(new InputStreamReader(response.body(), StandardCharsets.UTF_8))) {
        long started = System.nanoTime();
        assertThat(reader.readLine()).contains("first");
        assertThat((System.nanoTime() - started) / 1_000_000).isLessThan(600);
        assertThat(reader.readLine()).contains("done");
      }
    }
  }

  @AfterAll
  static void stop() {
    AI.stop(0);
  }
}
