terraform {
	required_providers {
		local =  {
			source = "hashicorp/local"
			version = "2.5.0"
			}
		null = {
			source = "hashicorp/null"
			version = "3.2.1"
			}
	}
}
provider "local" {}
provider "null" {}
resource "local_file" "test_txt" {
	filename = "${path.module}/test.txt"
	content = "Hello,world"
}
resource "null_resource" "git_commit" {
	depends_on = [local_file.test_txt]
	provisioner "local-exec" {
		command = <<-EOT
			git add test.txt
			git commit -m "First commit"
			EOT			
			}
}
