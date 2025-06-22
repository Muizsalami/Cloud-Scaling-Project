from locust import HttpUser, task, between

class WebsiteUser(HttpUser):
    wait_time = between(0, 0)

    @task
    def hit_homepage(self):
        self.client.get("/health")
